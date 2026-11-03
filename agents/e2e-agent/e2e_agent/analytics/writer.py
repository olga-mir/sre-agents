"""Deterministic BigQuery writes for investigation outcomes.

Not an LLM tool: the SQL is fixed and parameterized from InvestigationSummary
plus the session's code-tool calls (file hops only). Failures are logged and
swallowed so a warehouse outage cannot take down an investigation.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from .investigation_rows import (
    finding_row,
    investigation_id_for_session,
    investigation_row,
    path_hop_rows,
)

log = logging.getLogger("e2e_agent.analytics")

_TRUTHY = {"1", "true", "yes", "on"}
# A healthy MERGE on these tables finishes in a few seconds. Without a
# timeout, job.result() waits out BigQuery's own ~12 minute internal retry
# and the playground request dies first.
_BQ_RESULT_TIMEOUT_S = float(os.environ.get("E2E_AGENT_BQ_TIMEOUT_S", "30"))
# Give-up-and-cancel is best effort. It must not become the new place we wait.
_BQ_CANCEL_TIMEOUT_S = 5.0


def writes_enabled() -> bool:
    flag = os.environ.get("E2E_AGENT_BQ_WRITE", "1").strip().lower()
    if flag not in _TRUTHY:
        return False
    return bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))


def _config() -> tuple[str, str, str]:
    project = os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""
    dataset = os.environ.get("BQ_DATASET", "ci_agent_eval")
    location = os.environ.get("BQ_LOCATION", "us-central1")
    return project, dataset, location


def _table(project: str, dataset: str, name: str) -> str:
    return f"`{project}.{dataset}.{name}`"


def record_outcome(*, state: dict[str, Any], summary: dict[str, Any],
                   source: str = "graph",
                   trace: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """MERGE one investigation + finding, and replace that investigation's file hops.

    Findings are idempotent on (investigation_id, failure_id). Hops are replaced
    for the investigation so a later deep-dive write is the full hop path.
    """
    if not writes_enabled():
        return {"ok": False, "skipped": True}

    project, dataset, location = _config()
    if not project or not dataset:
        return {"ok": False, "skipped": True}

    gov = state.get("governance") or {}
    rc = state.get("run_context") or {}
    session_id = gov.get("session_id") or ""
    iid = investigation_id_for_session(session_id) if session_id else investigation_id_for_session(
        f"{rc.get('repo', 'unknown')}:{rc.get('run_id', 'unknown')}"
    )
    paused = not bool(state.get("engineer_feedback"))
    inv = investigation_row(
        investigation_id=iid,
        repo=rc.get("repo") or "unknown",
        run_id=str(rc.get("run_id") or summary.get("failure_id") or ""),
        session_id=session_id,
        trigger_source=gov.get("trigger_source") or "TEST_OPERATOR",
        paused=paused,
        source=source,
    )
    finding = finding_row(summary, investigation_id=iid)
    hops = path_hop_rows(trace or [], investigation_id=iid,
                         ingested_at=inv["ingested_at"])

    last: Optional[BaseException] = None
    for attempt in range(1, 4):
        try:
            _merge(project, dataset, location, inv, finding, hops)
            return {"ok": True, "investigation_id": iid, "hop_count": len(hops)}
        except Exception as exc:  # noqa: BLE001 - warehouse must not crash the graph
            last = exc
            log.warning("BigQuery outcome write failed (attempt %s/3): %s",
                        attempt, exc)
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
    return {"ok": False, "error": str(last), "investigation_id": iid}


def _wait(job: Any) -> None:
    """Block on a query job, then cancel it if it exceeds the timeout.

    `QueryJob.result()` with no timeout is `while not is_job_done(): pass`.
    Each pass is `jobs.get` then `jobs.getQueryResults`. The default
    `job_retry` treats `backendError` as retryable and will restart the query
    for up to 40 minutes (`_DEFAULT_JOB_DEADLINE`). `job_retry=None` turns
    that off; this writer already retries the whole merge itself.

    Cancel matters: a timed-out MERGE keeps running server-side and holds the
    table, so the retry would queue behind the job we already gave up on.

    The cancel itself needs bounding for the same reason the poll did.
    `_AsyncJob.cancel` defaults to `retry=DEFAULT_RETRY` (600s deadline, and
    `backendError` is in its predicate) and `timeout=None` (no socket timeout).
    It runs only when the backend is already unhealthy, which is exactly when
    that default spins - inside a thread nothing outside can cancel.
    """
    from google.cloud.bigquery.retry import DEFAULT_RETRY

    try:
        job.result(
            timeout=_BQ_RESULT_TIMEOUT_S,
            # Default RPC retry deadline is 10 minutes, and it treats
            # backendError as retryable. Cap it to the same budget as the poll.
            retry=DEFAULT_RETRY.with_deadline(_BQ_RESULT_TIMEOUT_S),
            job_retry=None,
        )
    except Exception:
        cancel = getattr(job, "cancel", None)
        if callable(cancel):
            try:
                cancel(retry=None, timeout=_BQ_CANCEL_TIMEOUT_S)
            except Exception:
                log.warning("could not cancel BigQuery job", exc_info=True)
        raise


def _merge(project: str, dataset: str, location: str,
           inv: dict[str, Any], finding: dict[str, Any],
           hops: list[dict[str, Any]]) -> None:
    from google.cloud import bigquery

    client = bigquery.Client(project=project, location=location)
    inv_table = _table(project, dataset, "investigations")
    find_table = _table(project, dataset, "findings")
    hop_table = _table(project, dataset, "path_hops")

    inv_sql = f"""
    MERGE {inv_table} T
    USING (SELECT
      @investigation_id AS investigation_id,
      @repo AS repo,
      @run_id AS run_id,
      @session_id AS session_id,
      @trigger_source AS trigger_source,
      @paused AS paused,
      @source AS source,
      CAST(NULL AS STRING) AS dump_path,
      TIMESTAMP(@ingested_at) AS ingested_at
    ) S
    ON T.investigation_id = S.investigation_id
    WHEN MATCHED THEN UPDATE SET
      repo = S.repo, run_id = S.run_id, session_id = S.session_id,
      trigger_source = S.trigger_source, paused = S.paused,
      source = S.source, ingested_at = S.ingested_at
    WHEN NOT MATCHED THEN INSERT (
      investigation_id, repo, run_id, session_id, trigger_source,
      paused, source, dump_path, ingested_at
    ) VALUES (
      S.investigation_id, S.repo, S.run_id, S.session_id, S.trigger_source,
      S.paused, S.source, S.dump_path, S.ingested_at
    )
    """
    find_sql = f"""
    MERGE {find_table} T
    USING (SELECT
      @investigation_id AS investigation_id,
      @failure_id AS failure_id,
      @verdict AS verdict,
      @failure_type AS failure_type,
      @failed_test AS failed_test,
      @failed_job_names AS failed_job_names,
      @confidence AS confidence,
      @audit_passed AS audit_passed,
      @exit_reason AS exit_reason,
      @one_line_summary AS one_line_summary,
      @assertion_expected AS assertion_expected,
      @assertion_actual AS assertion_actual,
      @offending_sha AS offending_sha,
      @offending_paths AS offending_paths,
      @offending_symbols AS offending_symbols,
      @baseline_search_ok AS baseline_search_ok,
      @baseline_present_on_base AS baseline_present_on_base,
      @baseline_present_on_head AS baseline_present_on_head,
      @baseline_supports_introduction AS baseline_supports_introduction,
      @flake_signal AS flake_signal,
      @reasoning AS reasoning,
      @audit_notes AS audit_notes,
      @budget_max_turns AS budget_max_turns,
      @budget_current_turn AS budget_current_turn,
      @evidence_count AS evidence_count,
      TIMESTAMP(@ingested_at) AS ingested_at
    ) S
    ON T.investigation_id = S.investigation_id AND T.failure_id = S.failure_id
    WHEN MATCHED THEN UPDATE SET
      verdict = S.verdict, failure_type = S.failure_type,
      failed_test = S.failed_test, failed_job_names = S.failed_job_names,
      confidence = S.confidence, audit_passed = S.audit_passed,
      exit_reason = S.exit_reason, one_line_summary = S.one_line_summary,
      assertion_expected = S.assertion_expected,
      assertion_actual = S.assertion_actual,
      offending_sha = S.offending_sha, offending_paths = S.offending_paths,
      offending_symbols = S.offending_symbols,
      baseline_search_ok = S.baseline_search_ok,
      baseline_present_on_base = S.baseline_present_on_base,
      baseline_present_on_head = S.baseline_present_on_head,
      baseline_supports_introduction = S.baseline_supports_introduction,
      flake_signal = S.flake_signal, reasoning = S.reasoning,
      audit_notes = S.audit_notes,
      budget_max_turns = S.budget_max_turns,
      budget_current_turn = S.budget_current_turn,
      evidence_count = S.evidence_count, ingested_at = S.ingested_at
    WHEN NOT MATCHED THEN INSERT (
      investigation_id, failure_id, verdict, failure_type, failed_test,
      failed_job_names, confidence, audit_passed, exit_reason,
      one_line_summary, assertion_expected, assertion_actual, offending_sha,
      offending_paths, offending_symbols, baseline_search_ok,
      baseline_present_on_base, baseline_present_on_head,
      baseline_supports_introduction, flake_signal, reasoning, audit_notes,
      budget_max_turns, budget_current_turn, evidence_count, ingested_at
    ) VALUES (
      S.investigation_id, S.failure_id, S.verdict, S.failure_type, S.failed_test,
      S.failed_job_names, S.confidence, S.audit_passed, S.exit_reason,
      S.one_line_summary, S.assertion_expected, S.assertion_actual, S.offending_sha,
      S.offending_paths, S.offending_symbols, S.baseline_search_ok,
      S.baseline_present_on_base, S.baseline_present_on_head,
      S.baseline_supports_introduction, S.flake_signal, S.reasoning, S.audit_notes,
      S.budget_max_turns, S.budget_current_turn, S.evidence_count, S.ingested_at
    )
    """

    inv_job = client.query(inv_sql, job_config=bigquery.QueryJobConfig(
        job_timeout_ms=int(_BQ_RESULT_TIMEOUT_S * 1000),
        query_parameters=_scalar_params(inv, (
            ("investigation_id", "STRING"),
            ("repo", "STRING"),
            ("run_id", "STRING"),
            ("session_id", "STRING"),
            ("trigger_source", "STRING"),
            ("paused", "BOOL"),
            ("source", "STRING"),
            ("ingested_at", "STRING"),
        )),
    ))
    _wait(inv_job)

    find_params = _scalar_params(finding, (
        ("investigation_id", "STRING"),
        ("failure_id", "STRING"),
        ("verdict", "STRING"),
        ("failure_type", "STRING"),
        ("failed_test", "STRING"),
        ("confidence", "FLOAT64"),
        ("audit_passed", "BOOL"),
        ("exit_reason", "STRING"),
        ("one_line_summary", "STRING"),
        ("assertion_expected", "STRING"),
        ("assertion_actual", "STRING"),
        ("offending_sha", "STRING"),
        ("baseline_search_ok", "BOOL"),
        ("baseline_present_on_base", "BOOL"),
        ("baseline_present_on_head", "BOOL"),
        ("baseline_supports_introduction", "BOOL"),
        ("flake_signal", "STRING"),
        ("reasoning", "STRING"),
        ("budget_max_turns", "INT64"),
        ("budget_current_turn", "INT64"),
        ("evidence_count", "INT64"),
        ("ingested_at", "STRING"),
    ))
    find_params.extend([
        bigquery.ArrayQueryParameter(
            "failed_job_names", "STRING", finding.get("failed_job_names") or []),
        bigquery.ArrayQueryParameter(
            "offending_paths", "STRING", finding.get("offending_paths") or []),
        bigquery.ArrayQueryParameter(
            "offending_symbols", "STRING", finding.get("offending_symbols") or []),
        bigquery.ArrayQueryParameter(
            "audit_notes", "STRING", finding.get("audit_notes") or []),
    ])
    find_job = client.query(find_sql, job_config=bigquery.QueryJobConfig(
        job_timeout_ms=int(_BQ_RESULT_TIMEOUT_S * 1000),
        query_parameters=find_params,
    ))
    _wait(find_job)

    # One script so a failed insert rolls back the delete. Re-running after a
    # deep dive replaces the hop path with the full session, including files
    # opened only on the second pass.
    hop_sql = f"""
    BEGIN TRANSACTION;
    DELETE FROM {hop_table}
    WHERE investigation_id = @investigation_id;

    INSERT INTO {hop_table}
      (investigation_id, seq, tool_name, path, sha, symbol, ingested_at)
    SELECT
      @investigation_id,
      CAST(JSON_VALUE(h, '$.seq') AS INT64),
      JSON_VALUE(h, '$.tool_name'),
      JSON_VALUE(h, '$.path'),
      JSON_VALUE(h, '$.sha'),
      JSON_VALUE(h, '$.symbol'),
      TIMESTAMP(@ingested_at)
    FROM UNNEST(JSON_QUERY_ARRAY(@hops_json)) AS h;
    COMMIT TRANSACTION;
    """
    hop_payload = [
        {"seq": h["seq"], "tool_name": h["tool_name"], "path": h["path"],
         "sha": h.get("sha"), "symbol": h.get("symbol")}
        for h in hops
    ]
    hop_job = client.query(hop_sql, job_config=bigquery.QueryJobConfig(
        job_timeout_ms=int(_BQ_RESULT_TIMEOUT_S * 1000),
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "investigation_id", "STRING", inv["investigation_id"]),
            bigquery.ScalarQueryParameter(
                "ingested_at", "STRING", inv["ingested_at"]),
            bigquery.ScalarQueryParameter(
                "hops_json", "STRING", json.dumps(hop_payload)),
        ],
    ))
    _wait(hop_job)


def _scalar_params(row: dict[str, Any], spec: tuple[tuple[str, str], ...]) -> list:
    from google.cloud import bigquery
    return [
        bigquery.ScalarQueryParameter(name, typ, row.get(name))
        for name, typ in spec
    ]
