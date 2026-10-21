"""Disk I/O rate and thread CPU split: the pure parts over synthetic samples, the struct
layouts pinned to the SDK headers, the report cells, and (on macOS) the real libproc path."""
from __future__ import annotations

import ctypes
import sys

import pytest

from proccity import diskio, threads
from proccity.diskio import DiskSampler
from proccity.layout import Proc
from proccity.rate import RateTracker
from proccity.report import (
    BUSY_DISK,
    Sources,
    build_report,
    busy_reasons,
    render_markdown,
)
from proccity.threads import ThreadSampler, ThreadSplit, split

MAC = sys.platform == "darwin"


def P(pid, name="p", cpu=0.0, **kw):
    return Proc(pid=pid, ppid=1, name=name, user="u", cpu=cpu, rss=1 << 20, threads=4,
                status="running", **kw)


# ---- rate tracker (shared by net and disk) -------------------------------------------------

def test_rate_tracker_needs_two_samples_and_ignores_reused_pids():
    rt = RateTracker()
    assert rt.rates_from({1: 1000, 2: 500}, 10.0) == {}            # first sighting: no rate
    assert rt.rates_from({1: 3000, 2: 100, 3: 7}, 12.0) == {1: 1000.0}  # 2 went down, 3 is new
    assert rt.rates_from({1: 3000}, 12.0) == {}                     # zero elapsed: no division


# ---- disk ----------------------------------------------------------------------------------

def test_rusage_struct_matches_the_sdk_layout():
    """<sys/resource.h>: 16-byte uuid, then 18 uint64s; the two disk fields are the last."""
    assert ctypes.sizeof(diskio._RusageInfoV2) == diskio.RUSAGE_INFO_V2_SIZE == 160
    assert diskio._RusageInfoV2.ri_diskio_bytesread.offset == 16 + 16 * 8
    assert diskio._RusageInfoV2.ri_diskio_byteswritten.offset == 16 + 17 * 8


def test_disk_sampler_disabled_measures_nothing():
    d = DiskSampler(enabled=False)
    assert d.source == "unavailable"
    assert d.sample([1, 2, 3]) == {}


def test_disk_sampler_rates_only_for_readable_pids(monkeypatch):
    """Denied pids are absent from the result, never 0."""
    d = DiskSampler(enabled=False)
    d.source = "psutil"
    counters = {1: 0, 2: 1 << 20}
    monkeypatch.setattr(diskio, "read_psutil", lambda pid: counters.get(pid))
    clock = iter([100.0, 102.0])
    monkeypatch.setattr(diskio.time, "monotonic", lambda: next(clock))
    assert d.sample([1, 2, 3]) == {}
    counters[1] = 4 << 20
    assert d.sample([1, 2, 3]) == {1: 2.0 * (1 << 20), 2: 0.0}


def test_disk_busy_reason_and_cells():
    procs = [P(1, "writer", disk_rate=3.0 * BUSY_DISK), P(2, "quiet", disk_rate=0.0),
             P(3, "denied")]                                  # disk_rate None
    assert busy_reasons(procs[0]) == ("disk 3.0 MiB/s",)
    assert busy_reasons(procs[1]) == () and busy_reasons(procs[2]) == ()
    rep = build_report(procs, set(), None, t=0.0, cores=1, mem_total=1,
                       sources=Sources(disk="rusage", threads="unavailable"))
    assert [r.pid for r in rep.unseen_busy] == [1]
    md = render_markdown(rep, "now")
    assert "| writer | 1 | user | unknown | 0% | 1.0 MiB | needs admin | 3.0 MiB/s | 4 |" in md
    assert "| denied | 3 | user | unknown | 0% | 1.0 MiB | needs admin | – | 4 |" in md
    assert "- Disk: bytes to and from storage" in md
    assert "the per-thread split is **not measured** here" in md
    assert rep.to_dict()["thresholds"]["disk"] == BUSY_DISK


# ---- threads -------------------------------------------------------------------------------

def test_threadinfo_struct_matches_the_sdk_layout():
    """<sys/proc_info.h> struct proc_threadinfo: 2 uint64, 8 int32, 64-char name = 112."""
    assert ctypes.sizeof(threads._ProcThreadInfo) == threads.PROC_THREADINFO_SIZE == 112
    assert threads._ProcThreadInfo.pth_cpu_usage.offset == 16
    assert threads._ProcThreadInfo.pth_name.offset == 48


def test_split_counts_moving_threads_and_the_busiest_share():
    prev = {1: 100, 2: 100, 3: 100, 9: 50}
    cur = {1: 100, 2: 400, 3: 200, 4: 10, 9: 20}   # 1 idle, 2+3 moved, 4 new, 9 reused (down)
    s = split(prev, cur)
    assert s == ThreadSplit(total=5, active=2, top_share=300 / 400)
    assert split({1: 5}, {1: 5}) == ThreadSplit(total=1, active=0, top_share=0.0)
    assert split({}, {}) == ThreadSplit(total=0, active=0, top_share=0.0)


def test_thread_sampler_needs_two_ticks_and_forgets_gone_pids(monkeypatch):
    t = ThreadSampler(enabled=False)
    t.source = "psutil"
    samples = {7: {1: 10, 2: 10}}
    monkeypatch.setattr(threads, "read_psutil", lambda pid: samples.get(pid))
    assert t.sample([7, 8]) == {}                                   # first tick: baseline only
    samples[7] = {1: 10, 2: 40, 3: 1}
    assert t.sample([7, 8]) == {7: ThreadSplit(total=3, active=1, top_share=1.0)}
    samples.pop(7)
    assert t.sample([8]) == {}                                      # 7 gone; nothing to compare


def test_thread_cells_in_markdown():
    procs = [P(1, "hot", cpu=50.0, threads_active=1, top_thread_share=0.98),
             P(2, "team", cpu=50.0, threads_active=8, top_thread_share=0.14),
             P(3, "idle", threads_active=0, top_thread_share=0.0),
             P(4, "nomeasure")]
    rep = build_report(procs, set(), None, t=0.0, cores=1, mem_total=1,
                       sources=Sources(threads="libproc"))
    md = render_markdown(rep, "now")
    assert "| 4 (1 hot, 98%) |" in md
    assert "| 4 (8 hot, 14%) |" in md
    assert "| idle | 3 | user | unknown | 0% | 1.0 MiB | needs admin | – | 4 (idle) |" in md
    assert "| nomeasure | 4 | user | unknown | 0% | 1.0 MiB | needs admin | – | 4 |" in md
    assert "- Threads: count, how many used CPU" in md


# ---- wiring --------------------------------------------------------------------------------

def test_enrich_stamps_disk_and_split_and_leaves_unmeasured_as_none():
    from proccity.server import enrich
    procs = [P(1), P(2), P(3)]
    out = {p.pid: p for p in enrich(procs, {}, set(), disk_rates={1: 9.5},
                                    splits={2: ThreadSplit(total=4, active=2, top_share=0.6)})}
    assert out[1].disk_rate == 9.5 and out[1].threads_active is None
    assert out[2].disk_rate is None and (out[2].threads_active, out[2].top_thread_share) == (2, 0.6)
    assert out[3].disk_rate is None and out[3].top_thread_share is None


def test_building_carries_the_new_fields_rounded():
    from proccity.layout import CityState, layout
    b = layout([P(1, disk_rate=12.345, threads_active=3, top_thread_share=0.33333)],
               CityState())[0]
    assert (b.disk_rate, b.threads_active, b.top_thread_share) == (12.3, 3, 0.333)
    assert layout([P(2)], CityState())[0].disk_rate is None


# ---- live, macOS only ----------------------------------------------------------------------

@pytest.mark.skipif(not MAC, reason="libproc is macOS")
def test_live_libproc_reads_own_process_and_refuses_pid_zero():
    import os
    d, t = DiskSampler(), ThreadSampler()
    assert d.source == "rusage" and t.source == "libproc"
    me = os.getpid()
    assert d.read(me) is not None and d.read(me) >= 0
    mine = t.read(me)
    assert mine and all(v >= 0 for v in mine.values())
    assert t.read(0) is None or t.read(0) == {}   # the kernel, not ours


@pytest.mark.skipif(not MAC, reason="libproc is macOS")
def test_live_thread_split_sees_this_process_burn_one_thread():
    import os
    import time
    t = ThreadSampler()
    me = os.getpid()
    assert t.sample([me]) == {}
    end = time.perf_counter() + 0.3
    while time.perf_counter() < end:      # one hot thread: this one
        pass
    s = t.sample([me])[me]
    assert s.total >= 1 and s.active >= 1 and s.top_share > 0.5
