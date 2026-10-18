"""Authenticate OTLP exports to telemetry.googleapis.com.

ADK's generic OTLP exporter sends spans, logs, and metrics with no
Authorization header. The exporter reads
OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER when it is constructed,
which is during `adk web` startup, before the agent module loads. A .pth
file imports this module first and points that variable at the entry point
below.
"""

from __future__ import annotations

import os

_PROVIDER = "e2e_agent_gcp"
_ENV = "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER"
_ENDPOINT_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
)
_GOOGLE_HOSTS = ("telemetry.googleapis.com", "telemetry.mtls.googleapis.com")


def google_telemetry_endpoint(value: str) -> bool:
    return any(host in value for host in _GOOGLE_HOSTS)


def install() -> None:
    """Select the GCP credential provider when an OTLP endpoint is Google's."""
    if os.environ.get(_ENV):
        return
    if any(google_telemetry_endpoint(os.environ.get(name, "")) for name in _ENDPOINT_VARS):
        os.environ[_ENV] = _PROVIDER


def gcp_session():
    """requests.Session that attaches an ADC bearer token. Used as an entry point."""
    import requests

    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return AuthorizedSession(credentials)
    except Exception:
        return requests.Session()


install()
