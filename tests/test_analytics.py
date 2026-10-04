from pathlib import Path

from ci_agent.analytics.github_rows import is_scan_workflow, job_row, run_row
from ci_agent.analytics.investigation_rows import (
    finding_row, path_hop_rows, rows_from_dump, session_trace,
)


def test_scan_filter():
    assert is_scan_workflow("GitHub Actions Scan")
    assert is_scan_workflow("Google GitHub Admin: Actions Workflow Security Scan")
    assert not is_scan_workflow("pr-workflow")
    assert not is_scan_workflow("govulncheck")


def test_run_and_job_rows():
    run = {
        "id": 34899310618,
        "name": "pr-workflow",
        "path": ".github/workflows/pr-workflow.yaml",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "head_branch": "pause-benchmark",
        "head_sha": "e3e28a41ebfe",
        "display_title": "Add pause lifecycle mode",
        "html_url": "https://github.com/agent-substrate/substrate/actions/runs/34899310618",
        "created_at": "2026-09-14T21:31:46Z",
        "updated_at": "2026-09-14T21:52:05Z",
        "run_started_at": "2026-09-14T21:31:46Z",
        "workflow_id": 279832845,
        "repository": {"full_name": "agent-substrate/substrate"},
        "head_repository": {"full_name": "msau42/substrate"},
        "actor": {"login": "msau42"},
    }
    row = run_row(run, ingested_at="2026-09-15T00:00:00+00:00")
    assert row["run_id"] == "34899310618"
    assert row["conclusion"] == "success"
    assert row["head_repo"] == "msau42/substrate"

    job = {
        "id": 104161139552,
        "name": "run-tests",
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-09-14T21:31:49Z",
        "completed_at": "2026-09-14T21:46:01Z",
        "runner_name": "GitHub Actions 1",
        "steps": [
            {"name": "Run go test", "conclusion": "success"},
            {"name": "Dump diagnostics on failure", "conclusion": "skipped"},
        ],
    }
    j = job_row(job, repo="agent-substrate/substrate", run_id="34899310618",
                ingested_at="2026-09-15T00:00:00+00:00")
    assert j["failed_steps"] == []
    assert j["step_count"] == 2
    assert j["duration_s"] == 852.0


def test_finding_and_path_hops_from_summary():
    summary = {
        "failure_id": "primary",
        "confidence": 0.9,
        "one_line_summary": "introduced",
        "exit_reason": "investigation_complete",
        "audit_passed": False,
        "audit_notes": ["introduction claim with no baseline check"],
        "budget": {"max_turns": 3, "current_turn": 1},
        "finding": {
            "verdict": "INTRODUCED_BY_COMMIT",
            "failure_type": "GINKGO_TEST",
            "failed_test": "TestFoo",
            "failed_job_names": ["run-tests"],
            "assertion_actual": "no free workers",
            "offending_paths": ["pkg/x.go"],
            "offending_symbols": ["buildDeploymentApplyConfig"],
            "baseline": None,
            "reasoning": "because",
            "evidence": [],
        },
    }
    row = finding_row(summary, investigation_id="local:1",
                      ingested_at="2026-09-15T00:00:00+00:00")
    assert row["audit_passed"] is False
    assert row["baseline_supports_introduction"] is None
    assert row["verdict"] == "INTRODUCED_BY_COMMIT"

    hops = path_hop_rows(
        [{"node": "investigator", "calls": [
            {"name": "list_failed_jobs", "args": {}},
            {"name": "adk_request_input", "args": {}},
            {"name": "outline_files", "args": {"targets": [
                {"path": "pkg/a.go", "sha": "abc"},
                {"path": "pkg/b.go", "sha": "def"},
                {"path": "", "sha": "nope"},
            ]}},
            {"name": "read_symbol", "args": {
                "path": "pkg/a.go", "sha": "abc", "symbol": "TestFoo"}},
            {"name": "changed_symbols", "args": {"path": "pkg/a.go"}},
        ]}],
        investigation_id="local:1",
        ingested_at="2026-09-15T00:00:00+00:00",
    )
    assert [(h["seq"], h["tool_name"], h["path"], h["symbol"]) for h in hops] == [
        (1, "outline_files", "pkg/a.go", None),
        (2, "outline_files", "pkg/b.go", None),
        (3, "read_symbol", "pkg/a.go", "TestFoo"),
        (4, "changed_symbols", "pkg/a.go", None),
    ]
    assert hops[0]["sha"] == "abc"
    assert hops[3]["sha"] is None
    assert {h["path"] for h in hops} == {"pkg/a.go", "pkg/b.go"}


def test_session_trace_keeps_only_named_calls():
    class Call:
        def __init__(self, name, args):
            self.name = name
            self.args = args

    class Event:
        author = "investigator"

        def __init__(self, calls):
            self._calls = calls

        def get_function_calls(self):
            return self._calls

    trace = session_trace([
        Event([]),
        Event([Call("read_symbol", {"path": "a.go", "sha": "s", "symbol": "T"})]),
    ])
    hops = path_hop_rows(trace, investigation_id="session:1",
                         ingested_at="2026-09-15T00:00:00+00:00")
    assert len(trace) == 1
    assert hops[0]["path"] == "a.go"
    assert hops[0]["symbol"] == "T"


def test_rows_from_dump(tmp_path: Path):
    d = tmp_path / "33259998849"
    d.mkdir()
    (d / "summaries.json").write_text(
        '{"primary": {"failure_id": "primary", "confidence": 0.5, '
        '"one_line_summary": "x", "exit_reason": "investigation_complete", '
        '"audit_passed": false, "finding": {"verdict": "INDETERMINATE"}}}'
    )
    (d / "trace.jsonl").write_text(
        '{"node": "sre_investigation", "calls": [], '
        '"output": {"repo": "agent-substrate/substrate", "run_id": "33259998849"}}\n'
        '{"node": "investigator", "calls": [{"name": "outline_file", '
        '"args": {"path": "a.go"}}], "output": null}\n'
    )
    mapped = rows_from_dump(d)
    assert mapped["investigations"][0]["repo"] == "agent-substrate/substrate"
    assert mapped["findings"][0]["verdict"] == "INDETERMINATE"
    assert mapped["path_hops"][0]["tool_name"] == "outline_file"
    assert mapped["path_hops"][0]["path"] == "a.go"


def test_writes_disabled_without_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("CI_AGENT_BQ_WRITE", "1")
    from ci_agent.analytics.writer import record_outcome, writes_enabled
    assert writes_enabled() is False
    assert record_outcome(state={}, summary={"failure_id": "p"})["skipped"] is True


def test_writes_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-gcp-project")
    monkeypatch.setenv("CI_AGENT_BQ_WRITE", "0")
    from ci_agent.analytics.writer import writes_enabled
    assert writes_enabled() is False


def test_merge_uses_parameterized_sql(monkeypatch):
    from ci_agent.analytics import writer

    class FakeJob:
        def result(self, timeout=None, **kwargs):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            FakeClient.calls.append([])

        def query(self, sql, job_config=None):
            FakeClient.calls[-1].append(sql)
            assert job_config is not None
            assert job_config.query_parameters
            if "path_hops" in sql:
                FakeClient.params.append(job_config.query_parameters)
            return FakeJob()

    FakeClient.calls = []
    FakeClient.params = []
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-gcp-project")
    monkeypatch.setenv("CI_AGENT_BQ_WRITE", "1")
    monkeypatch.setenv("BQ_DATASET", "ci_agent_eval")
    monkeypatch.setattr(writer, "_merge", writer._merge)
    import google.cloud.bigquery as bq
    monkeypatch.setattr(bq, "Client", FakeClient)

    result = writer.record_outcome(
        state={
            "governance": {"session_id": "abc", "trigger_source": "TEST_OPERATOR"},
            "run_context": {"repo": "o/r", "run_id": "99"},
        },
        summary={
            "failure_id": "primary",
            "confidence": 0.4,
            "one_line_summary": "x",
            "exit_reason": "investigation_complete",
            "audit_passed": False,
            "finding": {"verdict": "INDETERMINATE"},
        },
        trace=[{"node": "investigator", "calls": [
            {"name": "outline_file", "args": {"path": "pkg/x.go", "sha": "abc"}},
            {"name": "list_failed_jobs", "args": {}},
        ]}],
    )
    assert result["ok"] is True
    assert result["investigation_id"] == "session:abc"
    assert result["hop_count"] == 1
    assert len(FakeClient.calls[0]) == 3
    assert "MERGE" in FakeClient.calls[0][0]
    assert "findings" in FakeClient.calls[0][1]
    assert "path_hops" in FakeClient.calls[0][2]
    assert "tool_calls" not in " ".join(FakeClient.calls[0])
    hop_params = {p.name: p.value for p in FakeClient.params[0]}
    assert "pkg/x.go" in hop_params["hops_json"]


def test_write_failure_is_swallowed(monkeypatch):
    from ci_agent.analytics import writer

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-gcp-project")
    monkeypatch.setenv("CI_AGENT_BQ_WRITE", "1")
    monkeypatch.setattr(writer.time, "sleep", lambda _n: None)

    def boom(*_a, **_k):
        raise RuntimeError("bq down")

    monkeypatch.setattr(writer, "_merge", boom)
    result = writer.record_outcome(
        state={"governance": {"session_id": "x"},
               "run_context": {"repo": "o/r", "run_id": "1"}},
        summary={"failure_id": "primary"},
    )
    assert result["ok"] is False
    assert "bq down" in result["error"]


def test_bq_timeout_cancels_the_job(monkeypatch):
    from ci_agent.analytics import writer

    class FakeJob:
        def result(self, timeout=None, **kwargs):
            assert timeout == writer._BQ_RESULT_TIMEOUT_S
            assert kwargs.get("job_retry") is None
            raise TimeoutError("still running")

        def cancel(self, retry="unset", timeout=None):
            # The real cancel defaults to retry=DEFAULT_RETRY (600s, retries
            # backendError) and timeout=None. Both must be overridden, or the
            # give-up path becomes the new place the merge hangs.
            assert retry is None
            assert timeout == writer._BQ_CANCEL_TIMEOUT_S
            FakeJob.cancelled += 1

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def query(self, sql, job_config=None):
            return FakeJob()

    FakeJob.cancelled = 0
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-gcp-project")
    monkeypatch.setenv("CI_AGENT_BQ_WRITE", "1")
    monkeypatch.setattr(writer.time, "sleep", lambda _n: None)
    import google.cloud.bigquery as bq
    monkeypatch.setattr(bq, "Client", FakeClient)

    result = writer.record_outcome(
        state={"governance": {"session_id": "x"},
               "run_context": {"repo": "o/r", "run_id": "1"}},
        summary={"failure_id": "primary"},
    )
    assert result["ok"] is False
    assert "still running" in result["error"]
    # Three attempts, and each dies on the first MERGE, which is cancelled.
    assert FakeJob.cancelled == 3
