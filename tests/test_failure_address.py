from ci_agent.code.failure_address import (
    package_to_path, parse_go_failures, resolve_path, strip_timestamps,
)

MODULE = "github.com/agent-substrate/substrate"

LOG = """2026-08-29T15:23:07.3000325Z === RUN   TestResumeActor
2026-08-29T15:23:07.3000325Z     actor_test.go:3086: ResumeActor(actor-squatter) failed: rpc error: code = ResourceExhausted
2026-08-29T15:23:07.3001157Z --- FAIL: TestResumeActor (0.51s)
2026-08-29T15:23:07.3001157Z --- FAIL: TestResumeActor/missing_backend (0.01s)
2026-08-29T15:23:07.3616993Z FAIL\tgithub.com/agent-substrate/substrate/cmd/ateapi/internal/controlapi/functionaltest\t44.674s
2026-08-29T15:23:09.0000000Z main.(*Server).handleAuth(0xc000123456, 0x1)
"""


def test_strip_timestamps():
    assert strip_timestamps(LOG).splitlines()[0] == "=== RUN   TestResumeActor"


def test_test_and_subtest_addresses():
    addrs = parse_go_failures(LOG, MODULE)
    tests = [a for a in addrs if a.kind == "test"]
    assert [a.test_address for a in tests] == [
        "TestResumeActor", "TestResumeActor/missing_backend"]


def test_package_is_backfilled_from_the_trailing_fail_line():
    """Go prints the package AFTER the failure, so attribution is retroactive."""
    addrs = parse_go_failures(LOG, MODULE)
    assert all(a.package and a.package.endswith("functionaltest")
               for a in addrs if a.kind in ("test", "file_line"))


def test_file_line_address():
    fl = [a for a in parse_go_failures(LOG, MODULE) if a.kind == "file_line"][0]
    assert (fl.path_hint, fl.line) == ("actor_test.go", 3086)


def test_panic_frame_symbol_matches_outline_spelling():
    frames = [a for a in parse_go_failures(LOG, MODULE) if a.kind == "panic_frame"]
    assert frames[0].symbol == "(*Server).handleAuth"


def test_package_to_path():
    pkg = MODULE + "/cmd/ateapi/internal/controlapi"
    assert package_to_path(pkg, MODULE) == "cmd/ateapi/internal/controlapi"
    assert package_to_path(MODULE, MODULE) == ""
    assert package_to_path("other.com/x", MODULE) is None


def test_resolve_path_prefers_the_package_directory():
    assert resolve_path("actor_test.go", [], "cmd/ateapi") == "cmd/ateapi/actor_test.go"


def test_resolve_path_falls_back_to_a_unique_manifest_match():
    manifest = ["cmd/a/actor_test.go", "cmd/b/other_test.go"]
    assert resolve_path("actor_test.go", manifest) == "cmd/a/actor_test.go"
    assert resolve_path("other.go", manifest) is None


def test_ambiguous_basename_is_not_guessed():
    manifest = ["cmd/a/actor_test.go", "cmd/b/actor_test.go"]
    assert resolve_path("actor_test.go", manifest) is None
