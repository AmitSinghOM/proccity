"""How a process's CPU is spread over its threads: one hot thread, or many?

The number proccity already shows -- thread count -- says how big the crew is. This says how
many of them are actually working right now, and how much of the work the busiest one does.
"96 threads, 1 active, 100% in one" is a single-threaded job wearing a big coat; "12 threads,
11 active, 14% top" is a real parallel worker. Both are facts about the process, not verdicts.

Per-thread CPU time is cumulative, so this needs two samples: the threads whose time moved
are *active*; the largest move over the sum is the *top share*. Thread ids never leave this
module; the report carries two numbers per process.

Sources:

- **macOS** — `proc_pidinfo(PROC_PIDLISTTHREADS)` + `proc_pidinfo(PROC_PIDTHREADINFO)` from
  libproc via `ctypes`, the same calls `ps -M` and `top` use. psutil's `threads()` is *not*
  usable here: it needs `task_for_pid`, which macOS denies for every process but your own
  (measured: 1 ok, 910 denied). libproc answers for same-user pids with no root: ~550
  processes, ~3,200 threads, 0.12 s. Thread names come back in the struct and are discarded.
- **Linux, Windows, BSD** — psutil `Process.threads()`: `/proc/<pid>/task/*/stat` on Linux
  (own user), `GetThreadTimes` on Windows (own processes).

Other users' processes are a dash: the OS would not let this user look.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
from dataclasses import dataclass

import psutil

log = logging.getLogger("proccity.threads")

PROC_PIDTHREADINFO = 5
PROC_PIDLISTTHREADS = 6
MAXTHREADNAMESIZE = 64
MAX_THREADS = 4096          # one process with more threads than this is clipped, not failed


class _ProcThreadInfo(ctypes.Structure):
    """<sys/proc_info.h> struct proc_threadinfo; 112 bytes."""
    _fields_ = [
        ("pth_user_time", ctypes.c_uint64),
        ("pth_system_time", ctypes.c_uint64),
        ("pth_cpu_usage", ctypes.c_int32),
        ("pth_policy", ctypes.c_int32),
        ("pth_run_state", ctypes.c_int32),
        ("pth_flags", ctypes.c_int32),
        ("pth_sleep_time", ctypes.c_int32),
        ("pth_curpri", ctypes.c_int32),
        ("pth_priority", ctypes.c_int32),
        ("pth_maxpriority", ctypes.c_int32),
        ("pth_name", ctypes.c_char * MAXTHREADNAMESIZE),
    ]


PROC_THREADINFO_SIZE = 16 + 8 * 4 + MAXTHREADNAMESIZE


@dataclass(frozen=True)
class ThreadSplit:
    total: int          # threads seen in the latest sample
    active: int         # threads whose CPU time moved since the previous sample
    top_share: float    # share of the moved time in the busiest thread, 0..1; 0 when nothing moved


def split(prev: dict[int, int], cur: dict[int, int]) -> ThreadSplit:
    """Pure: two {thread id: cumulative cpu time} samples -> ThreadSplit.

    Threads new since `prev` are counted in `total` but cannot be active (no baseline).
    A thread whose time went down is a reused id; ignored.
    """
    deltas = [cur[t] - prev[t] for t in cur if t in prev and cur[t] > prev[t]]
    moved = sum(deltas)
    return ThreadSplit(total=len(cur), active=len(deltas),
                       top_share=(max(deltas) / moved) if moved else 0.0)


def _load_libproc() -> ctypes.CDLL | None:
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("c"))
        fn = lib.proc_pidinfo
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    return lib


def read_libproc(lib: ctypes.CDLL, pid: int) -> dict[int, int] | None:
    """{thread handle: user+system time} for `pid`, or None when the kernel refuses."""
    handles = (ctypes.c_uint64 * MAX_THREADS)()
    got = lib.proc_pidinfo(pid, PROC_PIDLISTTHREADS, 0, handles, ctypes.sizeof(handles))
    if got <= 0:
        return None
    out: dict[int, int] = {}
    info = _ProcThreadInfo()
    for i in range(got // 8):
        n = lib.proc_pidinfo(pid, PROC_PIDTHREADINFO, handles[i], ctypes.byref(info),
                             PROC_THREADINFO_SIZE)
        if n == PROC_THREADINFO_SIZE:      # a thread that exited between the two calls is skipped
            out[int(handles[i])] = int(info.pth_user_time) + int(info.pth_system_time)
    return out


def read_psutil(pid: int) -> dict[int, int] | None:
    try:
        threads = psutil.Process(pid).threads()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return None
    # psutil gives seconds as floats; nanoseconds as ints keeps the split arithmetic exact
    return {t.id: int((t.user_time + t.system_time) * 1e9) for t in threads}


class ThreadSampler:
    """`source`: libproc (macOS) | psutil (Linux/Windows/BSD) | unavailable."""

    def __init__(self, enabled: bool | None = None) -> None:
        self._last: dict[int, dict[int, int]] = {}
        self._lib: ctypes.CDLL | None = None
        self.source = "unavailable"
        if enabled is False:
            return
        if sys.platform == "darwin":
            if ctypes.sizeof(_ProcThreadInfo) != PROC_THREADINFO_SIZE:
                log.warning("proc_threadinfo layout mismatch; thread split not available")
                return
            self._lib = _load_libproc()
            if self._lib is not None:
                self.source = "libproc"
        elif hasattr(psutil.Process, "threads"):
            self.source = "psutil"

    def read(self, pid: int) -> dict[int, int] | None:
        if self.source == "libproc" and self._lib is not None:
            return read_libproc(self._lib, pid)
        if self.source == "psutil":
            return read_psutil(pid)
        return None

    def splits_from(self, samples: dict[int, dict[int, int]]) -> dict[int, ThreadSplit]:
        """Pure step: {pid: {tid: time}} for this tick -> splits for pids seen last tick too."""
        out = {pid: split(self._last[pid], cur) for pid, cur in samples.items()
               if pid in self._last}
        self._last = samples
        return out

    def sample(self, pids: list[int]) -> dict[int, ThreadSplit]:
        """A pid missing from the result was not measured (denied, or first sighting)."""
        if self.source == "unavailable":
            return {}
        samples: dict[int, dict[int, int]] = {}
        for pid in pids:
            cur = self.read(pid)
            if cur is not None:
                samples[pid] = cur
        return self.splits_from(samples)
