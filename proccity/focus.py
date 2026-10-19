"""Which processes the user can actually see.

Two facts per platform: the **frontmost** process, and the set of pids that **own a visible
window**. Every backend also says how much it knows, so the report can be honest about it:

| source           | platform         | needs                   | on-screen set means        |
|------------------|------------------|-------------------------|----------------------------|
| `quartz`         | macOS            | optional `proccity[mac]`| every visible window       |
| `frontmost-only` | macOS, no Quartz | nothing                 | just the frontmost app     |
| `win32`          | Windows          | nothing (ctypes/user32) | every visible top-level win|
| `x11`            | Linux on X11     | `/usr/bin/xprop`        | every mapped client window |
| `unknown`        | Wayland, headless| --                      | nothing: all "unseen"      |

Wayland has no generic window list -- each compositor decides -- so on Ubuntu 22.04+ (Wayland
by default) the answer is "unknown", not a guess. UWP apps on Windows report their owner as
ApplicationFrameHost.exe, a known quirk, so a UWP app's real process may show as unseen.
Window titles are never requested on any platform.

Subprocesses, same leash as the rest of proccity: absolute paths, literal argv, timeouts, no
shell, pure parsers. The only values fed back into a second command are validated against a
strict pattern first (an `ASN:0x…` token on macOS, a `0x…` window id on X11).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys

log = logging.getLogger("proccity.focus")

TIMEOUT = 2.0

# ---- macOS -----------------------------------------------------------------------------
LSAPPINFO = "/usr/bin/lsappinfo"
FRONT_ARGV = (LSAPPINFO, "front")
_ASN = re.compile(r"^ASN:0x[0-9a-fA-F]+-0x[0-9a-fA-F]+:?$")
_PID = re.compile(r'"pid"\s*=\s*(\d+)')

try:  # optional: pip install proccity[mac]
    import Quartz  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on hosts without pyobjc
    Quartz = None

# ---- Linux / X11 ---------------------------------------------------------------------------
XPROP = "/usr/bin/xprop"
XPROP_ROOT_ARGV = (XPROP, "-root", "_NET_CLIENT_LIST", "_NET_ACTIVE_WINDOW")
_WID = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
MAX_WINDOWS = 64        # one xprop call per window; cap keeps a tick bounded


def parse_asn(text: str) -> str | None:
    """`lsappinfo front` prints one token like `ASN:0x0-0x50050:`; anything else is rejected
    so a surprising output can never be fed back into the next command line."""
    token = text.strip()
    return token if _ASN.match(token) else None


def parse_pid(text: str) -> int | None:
    """`lsappinfo info -only pid <asn>` prints `"pid"=2543`."""
    m = _PID.search(text)
    return int(m.group(1)) if m else None


def parse_xprop_root(text: str) -> tuple[list[str], str | None]:
    """`xprop -root _NET_CLIENT_LIST _NET_ACTIVE_WINDOW` -> (window ids, active id).

    Lines look like `_NET_CLIENT_LIST(WINDOW): window id # 0x2a00003, 0x3000004` and
    `_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3000004`. Ids are kept only if they match the
    strict hex pattern, because they are fed back to `xprop -id`.
    """
    wids: list[str] = []
    active: str | None = None
    for line in text.splitlines():
        if "#" not in line:
            continue
        name, _, rest = line.partition("(")
        ids = [t.strip().rstrip(",") for t in rest.split("#", 1)[1].split(",")]
        ids = [i for i in ids if _WID.match(i)]
        if name.strip() == "_NET_CLIENT_LIST":
            wids = ids
        elif name.strip() == "_NET_ACTIVE_WINDOW" and ids:
            active = ids[0]
    return wids[:MAX_WINDOWS], active


def parse_xprop_window(text: str) -> tuple[int | None, bool]:
    """`xprop -id <wid> _NET_WM_PID _NET_WM_STATE` -> (pid, hidden).

    `_NET_WM_PID(CARDINAL) = 4242`; `_NET_WM_STATE(ATOM) = _NET_WM_STATE_HIDDEN, ...` marks a
    minimised window, which is not on screen.
    """
    pid: int | None = None
    hidden = False
    for line in text.splitlines():
        if line.startswith("_NET_WM_PID"):
            m = re.search(r"=\s*(\d+)", line)
            pid = int(m.group(1)) if m else None
        elif line.startswith("_NET_WM_STATE") and "_NET_WM_STATE_HIDDEN" in line:
            hidden = True
    return pid, hidden


def detect_source() -> str:
    """Which backend applies on this host, decided from the platform and the session."""
    if sys.platform == "darwin":
        return "quartz" if Quartz is not None else "frontmost-only"
    if sys.platform == "win32":
        return "win32"
    if sys.platform.startswith("linux"):
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or \
                os.environ.get("WAYLAND_DISPLAY"):
            return "unknown"
        if os.environ.get("DISPLAY") and os.path.exists(XPROP):
            return "x11"
    return "unknown"


def _run(argv: tuple[str, ...]) -> str | None:
    try:
        # Literal argv, absolute path, no shell: see the module docstring.
        proc = subprocess.run(argv, capture_output=True, text=True,  # noqa: S603
                              timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


class FocusSampler:
    """`source` says how the on-screen set was obtained; `unknown` means "do not trust it"."""

    def __init__(self, enabled: bool | None = None, source: str | None = None) -> None:
        if enabled is False:
            self.source = "unknown"
        else:
            self.source = source if source is not None else detect_source()
        self.enabled = self.source != "unknown"
        self._warned = False

    # -- macOS --------------------------------------------------------------------------
    def _mac_frontmost(self) -> int | None:
        asn = parse_asn(_run(FRONT_ARGV) or "")
        if asn is None:
            return None
        # The ASN was validated against a strict pattern above before it joins argv.
        return parse_pid(_run((LSAPPINFO, "info", "-only", "pid", asn)) or "")

    def _mac_screen(self) -> set[int]:
        front = self._mac_frontmost()
        pids: set[int] = {front} if front is not None else set()
        if self.source == "frontmost-only":
            if not self._warned:
                log.info("Quartz not installed (pip install 'proccity[mac]'); "
                         "on-screen set is the frontmost app only")
                self._warned = True
            return pids
        return pids | _quartz_window_owners()

    # -- Linux / X11 ----------------------------------------------------------------------
    def _x11_frontmost(self) -> int | None:
        _, active = parse_xprop_root(_run(XPROP_ROOT_ARGV) or "")
        if active is None:
            return None
        pid, _ = parse_xprop_window(_run((XPROP, "-id", active, "_NET_WM_PID")) or "")
        return pid

    def _x11_screen(self) -> set[int]:
        wids, _ = parse_xprop_root(_run(XPROP_ROOT_ARGV) or "")
        pids: set[int] = set()
        for wid in wids:                      # each wid matched _WID in the parser
            pid, hidden = parse_xprop_window(
                _run((XPROP, "-id", wid, "_NET_WM_PID", "_NET_WM_STATE")) or "")
            if pid is not None and not hidden:
                pids.add(pid)
        return pids

    # -- public ---------------------------------------------------------------------------
    def frontmost_pid(self) -> int | None:
        if not self.enabled:
            return None
        if self.source in ("quartz", "frontmost-only"):
            return self._mac_frontmost()
        if self.source == "win32":
            return win32_foreground_pid()
        if self.source == "x11":
            return self._x11_frontmost()
        return None

    def screen_pids(self) -> set[int]:
        """Pids owning an on-screen window. Empty when the source is unknown."""
        if not self.enabled:
            return set()
        try:
            if self.source in ("quartz", "frontmost-only"):
                return self._mac_screen()
            if self.source == "win32":
                front = win32_foreground_pid()
                pids = win32_visible_owner_pids()
                return pids | ({front} if front is not None else set())
            if self.source == "x11":
                return self._x11_screen()
        except Exception:  # a backend hiccup on the sampler thread must not stop the city
            log.debug("screen_pids failed for source %s", self.source, exc_info=True)
        return set()


def _quartz_window_owners() -> set[int]:
    """Owner pids of every on-screen window on the normal layer (0). Menu bar extras and
    overlays sit on higher layers and are not 'what the user is looking at'."""
    try:
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        windows = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    except Exception:  # a Quartz hiccup on the sampler thread must not stop the city
        log.debug("window list failed", exc_info=True)
        return set()
    return {int(w["kCGWindowOwnerPID"]) for w in windows
            if w.get("kCGWindowLayer", 0) == 0 and "kCGWindowOwnerPID" in w}


# ---- Windows (ctypes, no dependency) -------------------------------------------------------
# Kept as plain functions so the enumeration can be faked in tests on any platform.

def win32_visible_owner_pids(enum=None) -> set[int]:
    """Owner pids of visible, non-cloaked top-level windows.

    `enum` is injectable for tests: an iterable of (visible, cloaked, pid) tuples. On a real
    Windows host it walks EnumWindows with IsWindowVisible, DwmGetWindowAttribute(CLOAKED)
    and GetWindowThreadProcessId. Cloaked windows are the invisible UWP/virtual-desktop
    shells Explorer keeps around; they are not on screen.
    """
    if enum is None:
        if sys.platform != "win32":
            return set()
        enum = _win32_enumerate()
    return {pid for visible, cloaked, pid in enum if visible and not cloaked and pid > 0}


def win32_foreground_pid(getter=None) -> int | None:
    if getter is None:
        if sys.platform != "win32":
            return None
        getter = _win32_foreground
    pid = getter()
    return pid if isinstance(pid, int) and pid > 0 else None


def _win32_enumerate():  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
    DWMWA_CLOAKED = 14
    rows: list[tuple[bool, bool, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        visible = bool(user32.IsWindowVisible(hwnd))
        cloaked = wintypes.DWORD(0)
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                                     ctypes.sizeof(cloaked))
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rows.append((visible, bool(cloaked.value), int(pid.value)))
        return True

    user32.EnumWindows(cb, 0)
    return rows


def _win32_foreground() -> int | None:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None
