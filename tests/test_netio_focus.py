import subprocess

import pytest

from proccity import focus, netio
from proccity.focus import FocusSampler, parse_asn, parse_pid
from proccity.netio import NETTOP_ARGV, NetSampler, parse_nettop

# Captured from `nettop -P -L 1 -J bytes_in,bytes_out -x` on macOS 15. Names may contain
# dots; the pid is always the last dot-field.
FIXTURE = """,bytes_in,bytes_out,
launchd.1,0,0,
apsd.383,5592,11120,
mDNSResponder.449,968342,769937,
com.apple.WebKit.Networking.5120,1024,2048,
garbage line without pid
onlytwo.7,5,
notanumber.8,x,y,
"""


# ---- parser -----------------------------------------------------------------------------

def test_parse_sums_in_and_out_per_pid_and_keeps_dotted_names():
    got = parse_nettop(FIXTURE)
    assert got == {1: 0, 383: 16712, 449: 1738279, 5120: 3072}


def test_parse_skips_malformed_rows_without_guessing():
    assert 7 not in parse_nettop(FIXTURE) and 8 not in parse_nettop(FIXTURE)


def test_parse_rejects_output_with_the_wrong_header():
    assert parse_nettop("") == {}
    assert parse_nettop(",rx_packets,tx_packets,\nfoo.1,1,1,\n") == {}


# ---- rates from cumulative counters -----------------------------------------------------

def test_first_sample_is_zero_then_rate_is_delta_over_elapsed():
    s = NetSampler(enabled=False)
    assert s.rates_from({10: 1000, 20: 0}, now=100.0) == {}
    rates = s.rates_from({10: 3000, 20: 500}, now=102.0)
    assert rates == {10: 1000.0, 20: 250.0}


def test_a_new_pid_or_a_counter_that_went_backwards_reports_no_rate():
    s = NetSampler(enabled=False)
    s.rates_from({10: 5000}, now=0.0)
    rates = s.rates_from({10: 100, 11: 999}, now=1.0)     # pid 10 reused by a new process
    assert 10 not in rates and 11 not in rates
    assert s.rates_from({10: 200, 11: 1999}, now=2.0) == {10: 100.0, 11: 1000.0}


def test_zero_elapsed_time_does_not_divide_by_zero():
    s = NetSampler(enabled=False)
    s.rates_from({1: 1}, now=5.0)
    assert s.rates_from({1: 2}, now=5.0) == {}


# ---- the one subprocess, on a leash -----------------------------------------------------

def test_argv_is_literal_absolute_and_shell_free():
    assert NETTOP_ARGV[0] == "/usr/bin/nettop"
    assert all(isinstance(a, str) for a in NETTOP_ARGV)
    assert "-L" in NETTOP_ARGV and NETTOP_ARGV[NETTOP_ARGV.index("-L") + 1] == "1"


def test_disabled_sampler_never_runs_anything(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess must not run")
    monkeypatch.setattr(netio.subprocess, "run", boom)
    assert NetSampler(enabled=False).sample() == {}


def test_missing_tool_degrades_to_empty_and_warns_once(monkeypatch, caplog):
    def missing(*a, **k):
        raise FileNotFoundError("nettop")
    monkeypatch.setattr(netio.subprocess, "run", missing)
    s = NetSampler(enabled=True)
    with caplog.at_level("WARNING", logger="proccity.netio"):
        assert s.sample() == {} and s.sample() == {}
    assert sum("nettop unavailable" in r.message for r in caplog.records) == 1


def test_timeout_and_nonzero_exit_degrade_to_empty(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="nettop", timeout=1)
    monkeypatch.setattr(netio.subprocess, "run", slow)
    assert NetSampler(enabled=True).sample() == {}

    monkeypatch.setattr(netio.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "denied"))
    assert NetSampler(enabled=True).sample() == {}


def test_successful_run_feeds_the_parser_and_passes_the_literal_argv(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        assert kwargs.get("timeout") and not kwargs.get("shell")
        return subprocess.CompletedProcess(argv, 0, FIXTURE, "")
    monkeypatch.setattr(netio.subprocess, "run", fake_run)
    s = NetSampler(enabled=True)
    assert s.sample() == {}                     # first sample: counters only
    assert seen["argv"] == NETTOP_ARGV
    assert set(s._last) == {1, 383, 449, 5120}


# ---- focus parsers ----------------------------------------------------------------------

@pytest.mark.parametrize("text", ["ASN:0x0-0x50050:\n", "ASN:0x0-0x50050", "  ASN:0xA-0xB:  "])
def test_asn_parser_accepts_the_real_shape(text):
    assert parse_asn(text) is not None


@pytest.mark.parametrize("text", ["", "ASN:0x0-0x50050: --flag", "-v", "ASN:0x0-0x50050:;rm",
                                  "ASN:0xg-0x1:", "two\nlines"])
def test_asn_parser_rejects_anything_that_could_smuggle_an_argument(text):
    assert parse_asn(text) is None


def test_pid_parser():
    assert parse_pid('"LSDisplayName"="Kiro Crew"\n"pid"=2543\n') == 2543
    assert parse_pid('"pid" = 7') == 7
    assert parse_pid("no pid here") is None


def test_disabled_focus_reports_nothing_without_running_anything(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess must not run")
    monkeypatch.setattr(focus.subprocess, "run", boom)
    f = FocusSampler(enabled=False)
    assert f.frontmost_pid() is None and f.screen_pids() == set()


def test_without_quartz_the_screen_set_is_the_frontmost_app(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        out = "ASN:0x0-0x50050:\n" if argv[1] == "front" else '"pid"=2543\n'
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(focus.subprocess, "run", fake_run)
    monkeypatch.setattr(focus, "Quartz", None)
    f = FocusSampler(enabled=True)
    assert f.screen_pids() == {2543}
    assert calls[0] == ("/usr/bin/lsappinfo", "front")
    assert calls[1] == ("/usr/bin/lsappinfo", "info", "-only", "pid", "ASN:0x0-0x50050:")


def test_garbage_frontmost_output_never_reaches_the_second_command(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ASN:0x0-0x1: --evil\n", "")
    monkeypatch.setattr(focus.subprocess, "run", fake_run)
    assert FocusSampler(enabled=True).frontmost_pid() is None
    assert len(calls) == 1
