import subprocess

import pytest

from proccity import focus
from proccity.focus import (
    XPROP,
    FocusSampler,
    detect_source,
    parse_xprop_root,
    parse_xprop_window,
    win32_foreground_pid,
    win32_visible_owner_pids,
)
from proccity.interact import Counts, children_table, count_all, count_one

# Shapes from `xprop -root _NET_CLIENT_LIST _NET_ACTIVE_WINDOW` / `xprop -id <wid> ...` on an
# X11 session (EWMH). Ids are hex; a hidden window carries _NET_WM_STATE_HIDDEN.
ROOT = """_NET_CLIENT_LIST(WINDOW): window id # 0x2a00003, 0x3000004, 0x3800001
_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3000004
"""
WIN_VISIBLE = """_NET_WM_PID(CARDINAL) = 4242
_NET_WM_STATE(ATOM) = _NET_WM_STATE_FOCUSED
"""
WIN_HIDDEN = """_NET_WM_PID(CARDINAL) = 777
_NET_WM_STATE(ATOM) = _NET_WM_STATE_HIDDEN
"""
WIN_NOPID = "_NET_WM_STATE(ATOM) = \n"


# ---- X11 parsers -------------------------------------------------------------------------

def test_xprop_root_parser_returns_only_strict_hex_ids_and_the_active_one():
    wids, active = parse_xprop_root(ROOT)
    assert wids == ["0x2a00003", "0x3000004", "0x3800001"] and active == "0x3000004"


@pytest.mark.parametrize("bad", ["0x1; rm -rf /", "-id", "0xZZ", "0x" + "f" * 17, "12345"])
def test_xprop_root_parser_rejects_ids_that_could_smuggle_an_argument(bad):
    text = f"_NET_CLIENT_LIST(WINDOW): window id # {bad}, 0x10\n"
    wids, _ = parse_xprop_root(text)
    assert wids == ["0x10"]


def test_xprop_root_parser_caps_the_window_count_and_survives_garbage():
    many = ", ".join(f"0x{i:x}" for i in range(200))
    wids, active = parse_xprop_root(f"_NET_CLIENT_LIST(WINDOW): window id # {many}\n")
    assert len(wids) == focus.MAX_WINDOWS and active is None
    assert parse_xprop_root("") == ([], None)
    assert parse_xprop_root("_NET_CLIENT_LIST: no such atom\n") == ([], None)


def test_xprop_window_parser_reads_pid_and_hidden_state():
    assert parse_xprop_window(WIN_VISIBLE) == (4242, False)
    assert parse_xprop_window(WIN_HIDDEN) == (777, True)
    assert parse_xprop_window(WIN_NOPID) == (None, False)


def _x11_fake_run(calls):
    by_wid = {"0x2a00003": WIN_VISIBLE, "0x3000004": WIN_VISIBLE.replace("4242", "4243"),
              "0x3800001": WIN_HIDDEN}

    def fake_run(argv, **kw):
        calls.append(argv)
        out = ROOT if argv[1] == "-root" else by_wid[argv[2]]
        return subprocess.CompletedProcess(argv, 0, out, "")
    return fake_run


def test_x11_sampler_reports_visible_owners_and_the_active_window(monkeypatch):
    monkeypatch.setattr(focus.subprocess, "run", _x11_fake_run([]))
    f = FocusSampler(source="x11")
    assert f.source == "x11" and f.enabled
    assert f.screen_pids() == {4242, 4243}                    # 777 is hidden
    assert f.frontmost_pid() == 4243


def test_x11_sampler_feeds_only_parsed_ids_back_into_argv(monkeypatch):
    calls: list = []
    monkeypatch.setattr(focus.subprocess, "run", _x11_fake_run(calls))
    FocusSampler(source="x11").screen_pids()
    root_argv = (XPROP, "-root", "_NET_CLIENT_LIST", "_NET_ACTIVE_WINDOW")
    assert calls[0] == root_argv
    for argv in calls[1:]:
        assert argv[:2] == (XPROP, "-id")
        assert focus._WID.match(argv[2])


def test_x11_sampler_degrades_to_empty_when_xprop_is_missing(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("xprop")
    monkeypatch.setattr(focus.subprocess, "run", missing)
    f = FocusSampler(source="x11")
    assert f.screen_pids() == set() and f.frontmost_pid() is None




def test_x11_sampler_stops_querying_windows_once_the_time_budget_is_spent(monkeypatch):
    """Three windows, each xprop call 'takes' 0.6 s on a 1.0 s budget measured after the
    root call: two windows fit, the third is skipped, and the partial set is returned.
    Without the budget a hung X server costs MAX_WINDOWS timeouts per tick."""
    calls = []
    clock = [100.0]
    real_fake = _x11_fake_run(calls)

    def slow_run(argv, **kw):
        clock[0] += 0.6
        return real_fake(argv, **kw)
    monkeypatch.setattr(focus.subprocess, "run", slow_run)
    monkeypatch.setattr(focus.time, "monotonic", lambda: clock[0])
    f = FocusSampler(source="x11")
    pids = f.screen_pids()
    window_calls = [c for c in calls if c[1] == "-id"]
    assert len(window_calls) == 2, window_calls        # third window never queried
    assert pids == {4242, 4243} and f._slow is True
# ---- Win32 filter logic (enumeration injected; real EnumWindows is Windows-only) ---------

def test_win32_owner_pids_keep_visible_uncloaked_windows_only():
    rows = [(True, False, 100), (False, False, 200), (True, True, 300), (True, False, 0),
            (True, False, 100)]
    assert win32_visible_owner_pids(enum=rows) == {100}
    assert win32_visible_owner_pids(enum=[]) == set()


def test_win32_foreground_pid_validates_the_getter_result():
    assert win32_foreground_pid(getter=lambda: 4242) == 4242
    assert win32_foreground_pid(getter=lambda: 0) is None
    assert win32_foreground_pid(getter=lambda: None) is None
    assert win32_foreground_pid(getter=lambda: "4242") is None


def test_win32_helpers_return_empty_off_windows_without_touching_ctypes():
    import sys
    if sys.platform != "win32":
        assert win32_visible_owner_pids() == set() and win32_foreground_pid() is None


# ---- source detection -------------------------------------------------------------------

def test_wayland_session_is_reported_unknown_not_guessed(monkeypatch):
    monkeypatch.setattr(focus.sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("DISPLAY", ":0")                       # XWayland sets this too
    assert detect_source() == "unknown"
    monkeypatch.delenv("XDG_SESSION_TYPE")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert detect_source() == "unknown"


def test_x11_session_is_detected_only_with_display_and_xprop_present(monkeypatch):
    monkeypatch.setattr(focus.sys, "platform", "linux")
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(focus.os.path, "exists", lambda p: p == XPROP)
    assert detect_source() == "x11"
    monkeypatch.setattr(focus.os.path, "exists", lambda p: False)
    assert detect_source() == "unknown"
    monkeypatch.delenv("DISPLAY")
    assert detect_source() == "unknown"


def test_windows_and_mac_sources(monkeypatch):
    monkeypatch.setattr(focus.sys, "platform", "win32")
    assert detect_source() == "win32"
    monkeypatch.setattr(focus.sys, "platform", "darwin")
    monkeypatch.setattr(focus, "Quartz", None)
    assert detect_source() == "frontmost-only"
    monkeypatch.setattr(focus, "Quartz", object())
    assert detect_source() == "quartz"


def test_unknown_source_means_disabled_and_empty():
    f = FocusSampler(source="unknown")
    assert not f.enabled and f.screen_pids() == set() and f.frontmost_pid() is None
    assert FocusSampler(enabled=False).source == "unknown"


# ---- interaction counts -----------------------------------------------------------------

def test_children_table_counts_direct_children_only():
    assert children_table({1: 0, 10: 1, 11: 10, 12: 10, 20: 1}) == {0: 1, 1: 2, 10: 2}


def test_count_one_returns_none_pair_for_a_dead_pid():
    assert count_one(2_000_000_000) == (None, None)


def test_count_all_disabled_gives_children_but_no_io_counts():
    out = count_all({1: 0, 2: 1, 3: 1}, enabled=False)
    assert out[1] == Counts(files=None, sockets=None, children=2)
    assert out[2] == Counts(files=None, sockets=None, children=0)


def test_count_all_on_own_process_gives_real_numbers():
    import os
    out = count_all({os.getpid(): 1}, enabled=True)
    c = out[os.getpid()]
    assert isinstance(c.files, int) and c.files >= 0 and isinstance(c.sockets, int)
