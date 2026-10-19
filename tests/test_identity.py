import subprocess
from dataclasses import dataclass

from proccity import identity as idmod
from proccity.identity import (
    CODESIGN,
    UNKNOWN,
    UNVERIFIED,
    VERIFIED,
    Identity,
    IdentityChecker,
    classify,
    parse_display,
)

# Captured from `codesign -dv <pid>` / `codesign --verify <pid>` on macOS 26, 2026-10-19.
APPLE_APP = """Executable=/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder
Identifier=com.apple.finder
Format=app bundle with Mach-O universal (x86_64 arm64)
CodeDirectory v=20400 size=1234 flags=0x0(none) hashes=30+7 location=embedded
Signature size=4442
Authority=macOS Software Signing
Authority=Apple Code Signing Certification Authority
Authority=Apple Root CA
Info.plist entries=40
TeamIdentifier=not set
"""
APPLE_DAEMON = """Executable=/System/Library/PrivateFrameworks/MediaAnalysis.framework/mad
Identifier=com.apple.mediaanalysisd
Format=Mach-O thin (arm64)
CodeDirectory v=20400 size=6737 flags=0x0(none) hashes=200+7 location=embedded
Platform identifier=26
Signature size=4567
TeamIdentifier=not set
"""
DEVELOPER_ID = """Executable=/Applications/Firefox.app/Contents/MacOS/firefox
Identifier=org.mozilla.firefox
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20500 size=927 flags=0x10000(runtime) hashes=18+7 location=embedded
Signature size=8988
Timestamp=21 Sep 2026 at 8:23:56PM
Info.plist entries=37
TeamIdentifier=43AQ936H96
Runtime Version=26.5.0
"""
ADHOC = """Executable=/Users/someone/.kiro/crew/scratch/adhoc_py
Identifier=adhoc_py-555549444c4c44d255553144a103c7cbb8815741
Format=Mach-O thin (arm64)
CodeDirectory v=20400 size=330 flags=0x2(adhoc) hashes=4+2 location=embedded
Signature=adhoc
TeamIdentifier=not set
"""
INVALID_TEXT = "/Users/x/tampered: invalid signature (code or signature have been modified)\n"
UNSIGNED_TEXT = "/Users/x/raw: code object is not signed at all\n"
GONE_TEXT = "44139: No such process\n"


@dataclass(frozen=True)
class P:
    pid: int
    create_time: float = 1.0
    cpu: float = 0.0


# ---- parser ----------------------------------------------------------------------------

def test_display_parser_keeps_only_identity_fields_and_drops_the_executable_path():
    d = parse_display(DEVELOPER_ID)
    assert d["Identifier"] == "org.mozilla.firefox" and d["TeamIdentifier"] == "43AQ936H96"
    assert "Executable" not in d and "/Applications" not in str(d)
    assert parse_display(APPLE_APP)["authorities"] == [
        "macOS Software Signing", "Apple Code Signing Certification Authority", "Apple Root CA"]
    assert parse_display(APPLE_DAEMON)["Platform identifier"] == "26"
    assert parse_display("")["authorities"] == []


# ---- classification: all five real shapes ----------------------------------------------

def test_developer_id_is_verified_and_names_the_team():
    assert classify(0, "", parse_display(DEVELOPER_ID)) == Identity(VERIFIED, "43AQ936H96")


def test_apple_app_and_apple_platform_daemon_are_verified_as_apple():
    assert classify(0, "", parse_display(APPLE_APP)) == Identity(VERIFIED, "Apple")
    # platform binaries show no Authority lines by pid; the Platform identifier is the tell
    assert classify(0, "", parse_display(APPLE_DAEMON)) == Identity(VERIFIED, "Apple")


def test_adhoc_passes_verify_but_is_unverified_because_nobody_vouches():
    assert classify(0, "", parse_display(ADHOC)) == Identity(UNVERIFIED, "ad-hoc")


def test_invalid_and_unsigned_are_unverified_with_the_reason():
    assert classify(1, INVALID_TEXT, {}) == Identity(UNVERIFIED, "invalid")
    assert classify(1, UNSIGNED_TEXT, {}) == Identity(UNVERIFIED, "unsigned")
    assert classify(3, "some other failure", {}) == Identity(UNVERIFIED, "")


def test_gone_process_and_failed_run_are_unknown_never_unverified():
    assert classify(1, GONE_TEXT, {}).state == UNKNOWN
    assert classify(None, "", {}).state == UNKNOWN


def test_no_state_is_ever_called_safe():
    for rc, text, disp in ((0, "", parse_display(DEVELOPER_ID)), (0, "", parse_display(ADHOC)),
                           (1, INVALID_TEXT, {}), (None, "", {})):
        assert classify(rc, text, disp).state in (VERIFIED, UNVERIFIED, UNKNOWN)


# ---- checker: subprocess leash, cache, budget, priority ----------------------------------

def test_disabled_checker_never_runs_codesign(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess must not run")
    monkeypatch.setattr(idmod.subprocess, "run", boom)
    c = IdentityChecker(enabled=False)
    assert c.source == "unknown" and c.check(1) .state == UNKNOWN
    assert c.refresh([P(1)], lambda p: p.pid) == {1: Identity(UNKNOWN)}


def test_only_a_validated_positive_int_pid_reaches_argv(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", DEVELOPER_ID)
    monkeypatch.setattr(idmod.subprocess, "run", fake_run)
    c = IdentityChecker(enabled=True)
    assert c.check(3441) == Identity(VERIFIED, "43AQ936H96")
    assert seen == [(CODESIGN, "--verify", "3441"), (CODESIGN, "-dv", "3441")]
    assert c.check(0).state == UNKNOWN and c.check(-5).state == UNKNOWN
    assert c.check("3441; rm -rf /").state == UNKNOWN          # type: ignore[arg-type]
    assert len(seen) == 2                                     # nothing else ran


def test_verify_failure_skips_the_display_call(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, 1, "", INVALID_TEXT)
    monkeypatch.setattr(idmod.subprocess, "run", fake_run)
    assert IdentityChecker(enabled=True).check(7) == Identity(UNVERIFIED, "invalid")
    assert calls == ["--verify"]


def test_missing_codesign_degrades_to_unknown_and_warns_once(monkeypatch, caplog):
    def missing(*a, **k):
        raise FileNotFoundError("codesign")
    monkeypatch.setattr(idmod.subprocess, "run", missing)
    c = IdentityChecker(enabled=True)
    with caplog.at_level("WARNING", logger="proccity.identity"):
        assert c.check(1).state == UNKNOWN and c.check(2).state == UNKNOWN
    assert sum("codesign unavailable" in r.message for r in caplog.records) == 1


def test_refresh_respects_budget_caches_by_lifetime_and_forgets_the_dead(monkeypatch):
    checked = []

    def fake_run(argv, **kw):
        if argv[1] == "--verify":
            checked.append(int(argv[2]))
        return subprocess.CompletedProcess(argv, 0, "", DEVELOPER_ID)
    monkeypatch.setattr(idmod.subprocess, "run", fake_run)
    c = IdentityChecker(enabled=True, budget=2)
    procs = [P(10, cpu=1), P(20, cpu=9), P(30, cpu=5)]
    by_cpu_desc = lambda p: -p.cpu  # noqa: E731
    out = c.refresh(procs, by_cpu_desc)
    assert checked == [20, 30]                              # budget 2, highest priority first
    assert out[20].state == VERIFIED and out[10].state == UNKNOWN
    out = c.refresh(procs, by_cpu_desc)
    assert checked == [20, 30, 10] and all(v.state == VERIFIED for v in out.values())
    # same pid, new create_time = a new process: re-checked, never inherits the old verdict
    out = c.refresh([P(20, create_time=2.0)], by_cpu_desc)
    assert checked[-1] == 20 and len(c._cache) == 1            # 10 and 30 forgotten


def test_refresh_never_marks_a_process_unknown_as_verified_before_it_is_checked(monkeypatch):
    monkeypatch.setattr(idmod.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", DEVELOPER_ID))
    c = IdentityChecker(enabled=True, budget=1)
    out = c.refresh([P(1), P(2), P(3)], lambda p: p.pid)
    assert [out[i].state for i in (1, 2, 3)] == [VERIFIED, UNKNOWN, UNKNOWN]
