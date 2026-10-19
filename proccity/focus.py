"""Which processes the user can actually see.

Two facts, both available on macOS without root or any privacy permission:

- the **frontmost** application, from Apple's `/usr/bin/lsappinfo`;
- the set of pids that **own a visible window**, from the Quartz window list
  (`CGWindowListCopyWindowInfo`, via the optional `pyobjc-framework-Quartz`). Window *titles*
  need Screen Recording permission since Catalina; owner pids and on-screen state do not, and
  titles are never requested.

When Quartz is not installed the on-screen set degrades to just the frontmost app, which is
still true, only smaller. On other platforms both are empty and every building is "unseen" —
the report then says so rather than pretending.

`lsappinfo` is the second and last subprocess in proccity. Same leash as `netio`: absolute
path, literal argv, timeout, no shell, parsed by a pure function.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys

log = logging.getLogger("proccity.focus")

LSAPPINFO = "/usr/bin/lsappinfo"
FRONT_ARGV = (LSAPPINFO, "front")
TIMEOUT = 2.0
_ASN = re.compile(r"^ASN:0x[0-9a-fA-F]+-0x[0-9a-fA-F]+:?$")
_PID = re.compile(r'"pid"\s*=\s*(\d+)')

try:  # optional: pip install proccity[mac]
    import Quartz  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on hosts without pyobjc
    Quartz = None


def parse_asn(text: str) -> str | None:
    """`lsappinfo front` prints one token like `ASN:0x0-0x50050:`; anything else is rejected
    so a surprising output can never be fed back into the next command line."""
    token = text.strip()
    return token if _ASN.match(token) else None


def parse_pid(text: str) -> int | None:
    """`lsappinfo info -only pid <asn>` prints `"pid"=2543`."""
    m = _PID.search(text)
    return int(m.group(1)) if m else None


def _run(argv: tuple[str, ...]) -> str | None:
    try:
        # Literal argv, absolute path, no shell: see the module docstring.
        proc = subprocess.run(argv, capture_output=True, text=True,  # noqa: S603
                              timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


class FocusSampler:
    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = sys.platform == "darwin" if enabled is None else enabled
        self._warned = False

    def frontmost_pid(self) -> int | None:
        if not self.enabled:
            return None
        asn = parse_asn(_run(FRONT_ARGV) or "")
        if asn is None:
            return None
        # The ASN was validated against a strict pattern above before it joins argv.
        return parse_pid(_run((LSAPPINFO, "info", "-only", "pid", asn)) or "")

    def screen_pids(self) -> set[int]:
        """Pids owning an on-screen, normal-layer window. Falls back to {frontmost}."""
        front = self.frontmost_pid()
        pids: set[int] = {front} if front is not None else set()
        if not self.enabled:
            return pids
        if Quartz is None:
            if not self._warned:
                log.info("Quartz not installed (pip install 'proccity[mac]'); "
                         "on-screen set is the frontmost app only")
                self._warned = True
            return pids
        return pids | _quartz_window_owners()


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
