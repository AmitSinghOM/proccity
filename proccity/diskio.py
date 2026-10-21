"""Per-process disk I/O rate: bytes actually moved to or from storage, per second.

No OS lets an ordinary user read another user's disk counters, and none of them need root
for your own. So this column fills for the processes you own and shows a dash for the rest
(root daemons, other logins). A dash is "the OS would not let this user look", never 0.

Sources, chosen per platform:

- **macOS** — `proc_pid_rusage(pid, RUSAGE_INFO_V2)` from libproc, called through `ctypes`.
  psutil has no `io_counters` on macOS at all, but this is the call Activity Monitor and
  `top` use, it needs no root for same-user pids, and it answers for ~550 processes in well
  under a tenth of a second. It is a library call, not a subprocess: no argv, no shell.
  The struct is pinned to the V2 flavor (16-byte uuid + 18 `uint64`s, 160 bytes), which
  Apple has kept stable since 10.9; the size is asserted before the first call so a wrong
  layout fails loudly instead of reading garbage as bytes.
- **Linux, Windows, BSD** — psutil `Process.io_counters()`: `/proc/<pid>/io` on Linux (own
  user, via the ptrace read rule), `GetProcessIoCounters` on Windows (own processes).
- anything else — `unavailable`; the column is a dash everywhere.

Counters are cumulative since process start, so a rate needs two samples (see rate.py).
Measured on a quiet Mac: two processes writing in a 3-second window, so most rows read 0 —
that is the honest picture, and the ones that do not are exactly the ones worth a look.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
import time

import psutil

from .rate import RateTracker

log = logging.getLogger("proccity.diskio")

RUSAGE_INFO_V2 = 2
RUSAGE_INFO_V2_SIZE = 16 + 18 * 8


class _RusageInfoV2(ctypes.Structure):
    """<sys/resource.h> struct rusage_info_v2, field for field, so the offsets are explicit."""
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


def _load_libproc() -> ctypes.CDLL | None:
    """The libc on macOS re-exports libproc; return it with proc_pid_rusage typed, or None."""
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("c"))
        fn = lib.proc_pid_rusage
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    fn.restype = ctypes.c_int
    return lib


def read_rusage(lib: ctypes.CDLL, pid: int) -> int | None:
    """Bytes read + written by `pid` since it started, or None when the kernel refuses."""
    info = _RusageInfoV2()
    if lib.proc_pid_rusage(pid, RUSAGE_INFO_V2, ctypes.byref(info)) != 0:
        return None
    return int(info.ri_diskio_bytesread) + int(info.ri_diskio_byteswritten)


def read_psutil(pid: int) -> int | None:
    try:
        io = psutil.Process(pid).io_counters()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return None
    return int(io.read_bytes) + int(io.write_bytes)


class DiskSampler:
    """`source`: rusage (macOS libproc) | psutil (Linux/Windows/BSD) | unavailable."""

    def __init__(self, enabled: bool | None = None) -> None:
        self._rates = RateTracker()
        self._lib: ctypes.CDLL | None = None
        self.source = "unavailable"
        if enabled is False:
            return
        if sys.platform == "darwin":
            if ctypes.sizeof(_RusageInfoV2) != RUSAGE_INFO_V2_SIZE:
                log.warning("rusage_info_v2 layout mismatch; disk rates not available")
                return
            self._lib = _load_libproc()
            if self._lib is not None:
                self.source = "rusage"
        elif hasattr(psutil.Process, "io_counters"):
            self.source = "psutil"

    def read(self, pid: int) -> int | None:
        if self.source == "rusage" and self._lib is not None:
            return read_rusage(self._lib, pid)
        if self.source == "psutil":
            return read_psutil(pid)
        return None

    def rates_from(self, counters: dict[int, int], now: float) -> dict[int, float]:
        return self._rates.rates_from(counters, now)

    def sample(self, pids: list[int]) -> dict[int, float]:
        """{pid: bytes per second} for pids this user may read and that have two samples.

        A pid missing from the result means "not measured" (denied, or first sighting), and
        the caller must render it as a dash, not as 0.
        """
        if self.source == "unavailable":
            return {}
        counters: dict[int, int] = {}
        for pid in pids:
            total = self.read(pid)
            if total is not None:
                counters[pid] = total
        return self.rates_from(counters, time.monotonic())
