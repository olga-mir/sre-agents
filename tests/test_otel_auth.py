from ci_agent.otel_auth import google_telemetry_endpoint


def test_google_telemetry_hosts_need_credentials():
    assert google_telemetry_endpoint("https://telemetry.googleapis.com/v1/traces")
    assert google_telemetry_endpoint(
        "https://telemetry.mtls.googleapis.com/v1/metrics"
    )


def test_other_otlp_endpoints_are_left_alone():
    assert not google_telemetry_endpoint("http://localhost:4318")
    assert not google_telemetry_endpoint("")
