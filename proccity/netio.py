"""Per-process network rate, from Apple's `nettop`.

psutil can list a process's open connections but not its traffic. On macOS the one tool that
reports per-process bytes without root is `/usr/bin/nettop`, so this module shells out to it.
That is the only subprocess in proccity, and it is kept on a short leash:

- absolute path, literal argv, nothing from the user or the network reaches the command line;
- one short-lived call per sample with a timeout; no shell;
- output is parsed by a pure function (`parse_nettop`) that tests exercise on a fixture;
- on any failure, or on any other OS, the result is simply empty and every building reports
  a rate of 0. The city must never go dark because a helper tool is missing.

`nettop` reports bytes *since the process started*, so a rate needs two samples: this module
keeps the previous counters per pid and divides the difference by the elapsed time. The first
sample for a pid is always 0, the same way psutil's first cpu_percent is.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import time

log = logging.getLogger("proccity.netio")

NETTOP = "/usr/bin/nettop"
# -P: one row per process (not per socket). -L 1: one sample then exit. -x: raw numbers.
# -J: only the columns we read. The header line names the columns, so the parser checks it.
NETTOP_ARGV = (NETTOP, "-P", "-L", "1", "-J", "bytes_in,bytes_out", "-x")
TIMEOUT = 3.0
_ROW = re.compile(r"^(?P<name>.*)\.(?P<pid>\d+)$")


def parse_nettop(text: str) -> dict[int, int]:
    """`nettop -P -L 1 -J bytes_in,bytes_out -x` output -> {pid: bytes_in + bytes_out}.

    The format is CSV with a leading empty column: `,bytes_in,bytes_out,` then rows like
    `mDNSResponder.449,968342,769937,`. Process names may contain dots; the pid is the last
    dot-separated field. Unparseable rows are skipped, never guessed.
    """
    out: dict[int, int] = {}
    lines = text.splitlines()
    if not lines or "bytes_in" not in lines[0] or "bytes_out" not in lines[0]:
        return out
    for line in lines[1:]:
        parts = line.strip().strip(",").split(",")
        if len(parts) < 3:
            continue
        m = _ROW.match(parts[0])
        if not m:
            continue
        try:
            total = int(parts[1]) + int(parts[2])
        except ValueError:
            continue
        out[int(m.group("pid"))] = total
    return out


class NetSampler:
    """Turns successive cumulative byte counts into a per-pid rate in bytes/second.

    `source` tells the report how to read a 0: `nettop` = measured; `needs-admin` = this
    platform has no unprivileged per-process counter (Linux needs CAP_NET_RAW, Windows needs
    an admin ETW session), so nothing was measured; `unavailable` = macOS but nettop failed.
    """

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = sys.platform == "darwin" if enabled is None else enabled
        self.source = "nettop" if self.enabled else "needs-admin"
        self._last: dict[int, int] = {}
        self._last_t: float | None = None
        self._failed = False

    def rates_from(self, counters: dict[int, int], now: float) -> dict[int, float]:
        """Pure step: given fresh cumulative counters, return rates against the previous call."""
        rates: dict[int, float] = {}
        if self._last_t is not None:
            dt = now - self._last_t
            if dt > 0:
                for pid, total in counters.items():
                    prev = self._last.get(pid)
                    if prev is not None and total >= prev:   # a reused pid restarts at 0
                        rates[pid] = (total - prev) / dt
        self._last, self._last_t = counters, now
        return rates

    def sample(self) -> dict[int, float]:
        if not self.enabled:
            return {}
        try:
            # Literal argv, absolute path, no shell, no user input: see the module docstring.
            proc = subprocess.run(NETTOP_ARGV, capture_output=True, text=True,  # noqa: S603
                                  timeout=TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.source = "unavailable"
            if not self._failed:        # say it once, not every two seconds
                log.warning("nettop unavailable (%s); network rates will read 0", exc)
                self._failed = True
            return {}
        if proc.returncode != 0:
            self.source = "unavailable"
            if not self._failed:
                log.warning("nettop exited %d; network rates will read 0", proc.returncode)
                self._failed = True
            return {}
        self._failed = False
        self.source = "nettop"
        return self.rates_from(parse_nettop(proc.stdout), time.monotonic())
