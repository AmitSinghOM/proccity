"""Stress and no-leakage pass for the report feature.

Stress: the report is rebuilt on the sampler thread every tick and read on request threads;
the two must never see a half-built object, and a long run must not grow unbounded state.

No leakage: the report is meant to be pasted anywhere, so it must contain nothing the
snapshot did not already expose. Specifically no command lines, environment variables,
working directories, executable paths, window titles or remote addresses -- the things a
process monitor *could* read and this one deliberately does not. The tests below render a
report over hostile synthetic processes and over the live table, and grep both formats.
"""
from __future__ import annotations

import concurrent.futures as cf
import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer

import pytest

from proccity import focus, netio
from proccity.focus import FocusSampler
from proccity.layout import Proc
from proccity.netio import NETTOP_ARGV, NetSampler
from proccity.report import Row, build_report, render_markdown
from proccity.server import City, enrich, make_handler

ROW_FIELDS = {"pid", "ppid", "name", "user", "cpu", "rss", "net_rate", "threads", "status",
              "kind", "why", "identity", "signer", "files", "sockets", "children",
              "disk_rate", "threads_active", "top_thread_share"}
# Field names a process monitor could expose and this one must not. Checked against every key
# in the JSON report, at every depth.
FORBIDDEN_KEYS = {"cmdline", "cmd", "args", "argv", "exe", "executable", "path", "cwd",
                  "environ", "env", "title", "window", "windows", "raddr", "laddr", "remote",
                  "addr", "address", "ip", "port", "connections", "open_files", "uid"}
# Values that would mean a leak if they ever appeared in the Markdown.
LEAK_PATTERNS = [
    re.compile(r"/Users/\w"), re.compile(r"/home/\w"), re.compile(r"/private/"),
    re.compile(r"\b[A-Z_]{3,}=\S"),                       # KEY=value environment shape
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d+"),         # ip:port
    re.compile(r"(?i)secret|token=|password|api[_-]?key"),
]


def _get(port, path, host="127.0.0.1"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"Host": host})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


@pytest.fixture
def live_city():
    """A real City, sampling the real process table, with helpers OFF so the stress run does
    not fork nettop/lsappinfo a thousand times. The helpers have their own tests."""
    return City(interval=60, start=False, net=NetSampler(enabled=False),
                focus=FocusSampler(enabled=False))


# ---- stress ------------------------------------------------------------------------------

def test_hundred_concurrent_report_reads_during_live_ticks_all_succeed_and_are_whole(live_city):
    live_city.tick()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(live_city))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    stop = threading.Event()

    def ticker():
        while not stop.is_set():
            live_city.tick()

    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    try:
        paths = ["/report.json", "/report.md", "/api/snapshot"] * 34
        with cf.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda p: (p, *_get(port, p)), paths))
    finally:
        stop.set()
        t.join(timeout=5)
        srv.shutdown()
        srv.server_close()
    assert len(results) == 102 and all(status == 200 for _, status, _ in results)
    for path, _, body in results:
        if path.endswith(".json") or path.endswith("snapshot"):
            d = json.loads(body)                      # a torn write would not parse
            if path == "/report.json":
                assert d["totals"] == {k: len(d[k]) for k in
                                       ("on_screen", "unseen_busy", "unseen_idle")}
        else:
            assert body.startswith(b"# proccity report") and body.rstrip().endswith(
                b"never whether it is safe.")


def test_thousand_ticks_do_not_grow_sampler_state_or_leak_threads():
    """Both samplers keep only the previous sample. Feed them a churning pid space and
    check their state stays bounded by the live set, never by history."""
    net = NetSampler(enabled=False)
    for i in range(1000):
        live = {pid: pid * 7 + i for pid in range(i, i + 50)}   # 50 live pids, all churning
        net.rates_from(live, now=float(i))
        assert len(net._last) == 50
    from proccity.diskio import DiskSampler
    from proccity.threads import ThreadSampler
    disk, thr = DiskSampler(enabled=False), ThreadSampler(enabled=False)
    for i in range(1000):
        live = {pid: pid * 7 + i for pid in range(i, i + 50)}
        disk.rates_from(live, now=float(i))
        thr.splits_from({pid: {1: i, 2: i * 2} for pid in live})
        assert len(disk._rates.last) == 50 and len(thr._last) == 50
    f = FocusSampler(enabled=False)
    before = threading.active_count()
    for _ in range(1000):
        assert f.screen_pids() == set() and f.frontmost_pid() is None
    assert threading.active_count() == before


def test_report_build_is_fast_enough_for_a_two_second_tick():
    """A 2,000-process table (four times a busy Mac) must build and render well inside the
    default sampling interval, on the sampler thread, without blocking requests."""
    import time
    procs = [Proc(pid, 1 if pid % 10 else 0, f"proc{pid}", "u", float(pid % 100),
                  (pid % 50) << 20, 4, "sleeping", net_rate=float(pid % 1000))
             for pid in range(2, 2002)]
    screen = {pid for pid in range(2, 2002, 25)}
    t0 = time.perf_counter()
    rep = build_report(procs, screen, 2, t=0.0, cores=8, mem_total=1 << 34)
    md = render_markdown(rep, "now")
    json.dumps(rep.to_dict())
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5, f"report took {elapsed:.2f}s for 2000 procs"
    assert md and sum(rep.to_dict()["totals"].values()) == 2000


# ---- no leakage --------------------------------------------------------------------------

def _all_keys(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _all_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _all_keys(v, out)


def test_report_rows_carry_exactly_the_allowlisted_fields_and_no_forbidden_key_anywhere():
    assert set(Row.__dataclass_fields__) == ROW_FIELDS
    procs = [Proc(10, 1, "a", "u", 1.0, 1, 1, "r"), Proc(20, 1, "b", "u", 99.0, 1 << 31, 1, "r")]
    d = build_report(procs, {10}, 10, t=0.0, cores=1, mem_total=1).to_dict()
    keys: set[str] = set()
    _all_keys(d, keys)
    assert not (keys & FORBIDDEN_KEYS), keys & FORBIDDEN_KEYS
    for bucket in ("on_screen", "unseen_busy", "unseen_idle"):
        for row in d[bucket]:
            assert set(row) == ROW_FIELDS


def test_hostile_process_names_cannot_inject_or_leak_into_markdown():
    """A process can be named almost anything. The Markdown must stay a table: pipes
    escaped, newlines flattened, and nothing in the name is interpreted."""
    hostile = [
        Proc(50, 1, "evil|name|", "u", 50.0, 1, 1, "r"),
        Proc(51, 1, "two\nlines", "u", 50.0, 1, 1, "r"),
        Proc(52, 1, "# heading\n## another", "u", 50.0, 1, 1, "r"),
        Proc(53, 1, "<script>alert(1)</script>", "u", 50.0, 1, 1, "r"),
        Proc(54, 1, "AWS_SECRET_ACCESS_KEY=abc", "u", 50.0, 1, 1, "r"),
    ]
    rep = build_report(hostile, set(), None, t=0.0, cores=1, mem_total=1)
    md = render_markdown(rep, "now")
    body = md.split("## Unseen & busy")[1].split("## Unseen & idle")[0]
    rows = [ln for ln in body.splitlines() if ln.startswith("| ")]
    assert len(rows) == 1 + len(hostile), "a newline in a name split a table row"
    assert all(ln.count("|") - ln.count("\\|") == 14 and ln.endswith("|") for ln in rows[1:])
    assert "| evil\\|name\\| |" in md and "two lines" in md
    assert "\n# heading" not in md and "\n## another" not in md
    # The report is text/markdown; a name is data. No key=value heuristics on our side either:
    # the name is shown as-is (escaped), never parsed, so an env-looking name is still a name.
    assert "AWS_SECRET_ACCESS_KEY=abc" in md
    # JSON round-trips the raw name untouched -- no interpretation, no truncation.
    d = json.loads(json.dumps(rep.to_dict()))
    assert {r["name"] for r in d["unseen_busy"]} == {p.name for p in hostile}


def test_live_report_contains_no_paths_addresses_or_env_shapes(live_city):
    """Against the real process table: whatever is running on this machine, the Markdown must
    not contain anything that looks like a filesystem path, an ip:port, or an environment
    variable. Process *names* are basenames, so a hit here is a leak, not a name."""
    live_city.tick()
    md = live_city.report_markdown_bytes().decode()
    for pat in LEAK_PATTERNS:
        hits = pat.findall(md)
        assert not hits, f"{pat.pattern!r} matched {hits[:3]}"
    d = json.loads(live_city.report_json_bytes())
    keys: set[str] = set()
    _all_keys(d, keys)
    assert not (keys & FORBIDDEN_KEYS)


def test_helpers_only_ask_for_what_the_report_shows():
    """nettop can report remote addresses and per-socket detail; lsappinfo can dump every
    launch-services attribute. proccity asks each for the minimum and nothing else."""
    assert "-P" in NETTOP_ARGV                       # per process, never per socket/address
    assert NETTOP_ARGV[NETTOP_ARGV.index("-J") + 1] == "bytes_in,bytes_out"
    assert not any(a in NETTOP_ARGV for a in ("-n", "-m", "route", "state", "interface"))
    assert focus.FRONT_ARGV == ("/usr/bin/lsappinfo", "front")
    # Only "pid" is requested from lsappinfo info, so names, bundle paths and exe paths never
    # enter the process even transiently.
    with open(focus.__file__, encoding="utf-8") as fh:
        src = fh.read()
    assert '"info", "-only", "pid"' in src


def test_helper_failures_never_reach_the_http_response(monkeypatch):
    """A crashing helper is logged, not served. The report must not carry exception text,
    tool stderr, or the tool's path."""
    def explode(*a, **k):
        raise OSError("nettop: /secret/path/leaked: Operation not permitted")
    monkeypatch.setattr(netio.subprocess, "run", explode)
    monkeypatch.setattr(focus.subprocess, "run", explode)
    city = City(interval=60, start=False, net=NetSampler(enabled=True),
                focus=FocusSampler(enabled=True))
    city.tick()
    for body in (city.report_json_bytes(), city.report_markdown_bytes(), city.snapshot_bytes()):
        assert b"secret" not in body and b"Operation not permitted" not in body
        assert b"Traceback" not in body and b"/usr/bin/" not in body


def test_enrich_never_marks_on_screen_without_a_screen_pid_and_rates_cannot_be_negative():
    procs = [Proc(1, 0, "launchd", "root", 0.0, 1, 1, "r"), Proc(2, 1, "x", "u", 0.0, 1, 1, "r")]
    assert all(not p.on_screen for p in enrich(procs, {}, set()))
    s = NetSampler(enabled=False)
    s.rates_from({2: 100}, 0.0)
    assert all(v >= 0 for v in s.rates_from({2: 50}, 1.0).values())     # counter reset -> drop
    assert all(v >= 0 for v in s.rates_from({2: 60}, 2.0).values())


# ---- new fields: identity, signer, counts, sources ----------------------------------------

TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
SIGNER_WORDS = {"", "Apple", "ad-hoc", "unsigned", "invalid"}
SOURCES = {"screen": {"quartz", "frontmost-only", "win32", "x11", "unknown"},
           "net": {"nettop", "needs-admin", "unavailable"},
           "identity": {"codesign", "unknown"},
           "disk": {"rusage", "psutil", "unavailable"},
           "threads": {"libproc", "psutil", "unavailable"}}


def test_live_identity_and_counts_fields_hold_no_paths_or_free_text(live_city):
    """The identity check reads codesign output that names executable paths; the counts read
    psutil data that names files and peers. None of that may survive into a row."""
    from proccity.identity import IdentityChecker
    city = City(interval=60, start=False, net=NetSampler(enabled=False),
                focus=FocusSampler(enabled=False), identity=IdentityChecker(budget=40))
    for _ in range(3):
        city.tick()
    d = json.loads(city.report_json_bytes())
    rows = d["on_screen"] + d["unseen_busy"] + d["unseen_idle"]
    assert any(r["identity"] == "verified" for r in rows), "expected some verified rows on macOS"
    for r in rows:
        assert r["identity"] in ("verified", "unverified", "unknown")
        assert r["signer"] in SIGNER_WORDS or TEAM_ID.match(r["signer"]), r["signer"]
        assert "/" not in r["signer"] and "\\" not in r["signer"]
        assert r["files"] is None or isinstance(r["files"], int)
        assert r["sockets"] is None or isinstance(r["sockets"], int)
        assert isinstance(r["children"], int)
        _assert_measured_or_none(r)
    for key, allowed in SOURCES.items():
        assert d["sources"][key] in allowed


def _assert_measured_or_none(r: dict) -> None:
    """The three newest fields are a number when measured and None when not -- never a 0
    standing in for 'could not look', and the share is a proper fraction."""
    disk, active, share = r["disk_rate"], r["threads_active"], r["top_thread_share"]
    assert disk is None or (isinstance(disk, float) and disk >= 0)
    assert active is None or 0 <= active <= r["threads"]
    assert share is None or 0.0 <= share <= 1.0


def test_report_sources_and_placeholders_are_honest_when_nothing_is_measured():
    from proccity.report import Sources, build_report, render_markdown
    procs = [Proc(10, 1, "a", "u", 9.0, 1, 1, "r")]
    rep = build_report(procs, set(), None, t=0.0, cores=1, mem_total=1,
                       sources=Sources(screen="unknown", net="needs-admin", identity="unknown"))
    md = render_markdown(rep, "now")
    assert "On-screen set: **unknown**" in md and "Network: **not measured**" in md
    assert "Identity: not checked" in md
    assert "| needs admin |" in md and "| 0 B/s |" not in md      # never a fake zero
    assert "| unknown |" in md and "| – | – |" in md                # id unknown, counts denied
    assert "safe" not in md.lower().replace("never whether it is safe", "")
