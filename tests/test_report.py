import json

from proccity.layout import Proc
from proccity.report import (
    BUSY_CPU,
    BUSY_NET,
    BUSY_RSS,
    IDLE_ROWS,
    build_report,
    busy_reasons,
    expand_to_families,
    fmt_bytes,
    fmt_rate,
    kind_of,
    render_markdown,
)


def P(pid, ppid, name="p", cpu=0.0, rss=50 << 20, net=0.0, threads=4, user="u",
      status="sleeping"):
    return Proc(pid=pid, ppid=ppid, name=name, user=user, cpu=cpu, rss=rss, threads=threads,
                status=status, net_rate=net)


# A small Mac: a browser family on screen, a sync daemon and a security agent working unseen,
# a shell with a quiet child, and a pile of idle daemons.
BROWSER, HELPER1, HELPER2 = 100, 101, 102
CLOUDD, CROWDSTRIKE, SHELL, LS = 200, 300, 400, 401
IDLE = list(range(500, 520))


def city():
    return [
        P(BROWSER, 1, "firefox", cpu=12.0, rss=800 << 20, net=20 * 1024),
        P(HELPER1, BROWSER, "firefox helper", cpu=40.0, rss=300 << 20),
        P(HELPER2, HELPER1, "firefox gpu", cpu=0.5),
        P(CLOUDD, 1, "cloudd", net=200 * 1024),                        # busy: network
        P(CROWDSTRIKE, 1, "com.crowdstrike.falcon.Agent", cpu=9.0),   # busy: cpu
        P(SHELL, 1, "zsh"),
        P(LS, SHELL, "ls", rss=2 << 30),                               # busy: memory
        *[P(pid, 1, f"daemon{pid}") for pid in IDLE],
    ]


# ---- family expansion -------------------------------------------------------------------

def test_descendants_of_an_on_screen_app_are_on_screen_too():
    vis = expand_to_families(city(), {BROWSER})
    assert vis == {BROWSER, HELPER1, HELPER2}


def test_unrelated_processes_are_not_pulled_in_and_empty_set_stays_empty():
    assert SHELL not in expand_to_families(city(), {BROWSER})
    assert expand_to_families(city(), set()) == set()


def test_expansion_survives_cycles_and_missing_parents():
    cyc = [P(5, 6), P(6, 5), P(7, 99)]
    assert expand_to_families(cyc, {5}) == {5, 6}
    assert expand_to_families(cyc, {7}) == {7}


# ---- buckets -----------------------------------------------------------------------------

def test_three_buckets_partition_every_process_exactly_once():
    procs = city()
    rep = build_report(procs, {BROWSER}, BROWSER, t=0.0, cores=8, mem_total=16 << 30)
    pids = [r.pid for r in rep.on_screen + rep.unseen_busy + rep.unseen_idle]
    assert sorted(pids) == sorted(p.pid for p in procs)
    assert {r.pid for r in rep.on_screen} == {BROWSER, HELPER1, HELPER2}
    assert {r.pid for r in rep.unseen_busy} == {CLOUDD, CROWDSTRIKE, LS}
    assert {r.pid for r in rep.unseen_idle} == {SHELL, *IDLE}


def test_each_busy_row_says_why_and_idle_rows_do_not():
    rep = build_report(city(), {BROWSER}, BROWSER, t=0.0, cores=8, mem_total=16 << 30)
    why = {r.pid: r.why for r in rep.unseen_busy}
    assert why[CLOUDD] == (f"net {fmt_rate(200 * 1024)}",)
    assert why[CROWDSTRIKE] == ("cpu 9%",)
    assert why[LS] == (f"mem {fmt_bytes(2 << 30)}",)
    assert all(r.why == () for r in rep.unseen_idle + rep.on_screen)


def test_thresholds_are_inclusive_and_an_on_screen_hog_is_not_called_unseen():
    assert busy_reasons(P(1, 1, cpu=BUSY_CPU)) == (f"cpu {BUSY_CPU:.0f}%",)
    assert busy_reasons(P(1, 1, cpu=BUSY_CPU - 0.1)) == ()
    assert busy_reasons(P(1, 1, net=BUSY_NET)) and not busy_reasons(P(1, 1, net=BUSY_NET - 1))
    assert busy_reasons(P(1, 1, rss=BUSY_RSS)) and not busy_reasons(P(1, 1, rss=BUSY_RSS - 1))
    rep = build_report([P(9, 1, "hog", cpu=300.0)], {9}, 9, t=0.0, cores=8, mem_total=1)
    assert [r.pid for r in rep.on_screen] == [9] and rep.unseen_busy == []


def test_no_screen_information_means_everything_is_unseen_and_frontmost_is_none():
    rep = build_report(city(), set(), None, t=0.0, cores=8, mem_total=1)
    assert rep.on_screen == [] and rep.frontmost is None
    assert BROWSER in {r.pid for r in rep.unseen_busy}


def test_busy_rows_sort_most_active_first_and_ties_by_pid():
    rep = build_report(city(), {BROWSER}, BROWSER, t=0.0, cores=8, mem_total=1)
    # LS holds 2 GiB (2.0), cloudd moves 200 KiB/s (~0.2), crowdstrike 9% cpu (0.09)
    assert [r.pid for r in rep.unseen_busy] == [LS, CLOUDD, CROWDSTRIKE]
    idle = [r.pid for r in rep.unseen_idle]
    assert idle == sorted(idle)   # identical activity -> pid order, stable across runs


def test_frontmost_is_the_matching_on_screen_row():
    rep = build_report(city(), {BROWSER}, BROWSER, t=0.0, cores=8, mem_total=1)
    assert rep.frontmost is not None and rep.frontmost.name == "firefox"
    rep2 = build_report(city(), {BROWSER}, 999, t=0.0, cores=8, mem_total=1)
    assert rep2.frontmost is None   # a frontmost pid we never saw is reported as unknown


# ---- kind labels -------------------------------------------------------------------------

def test_kind_is_a_label_not_a_filter():
    assert kind_of("com.crowdstrike.falcon.Agent") == "security"
    assert kind_of("TaniumClient") == "security"
    assert kind_of("mDNSResponder") == "system"
    assert kind_of("MDWORKER_SHARED") == "system"       # case-insensitive prefix
    assert kind_of("firefox") == "user"
    rep = build_report(city(), {BROWSER}, BROWSER, t=0.0, cores=8, mem_total=1)
    kinds = {r.pid: r.kind for r in rep.unseen_busy}
    assert kinds[CROWDSTRIKE] == "security" and kinds[CLOUDD] == "system"
    assert "does not judge" in rep.note


# ---- rendering ---------------------------------------------------------------------------

def test_json_shape_is_stable_and_serialisable():
    rep = build_report(city(), {BROWSER}, BROWSER, t=1.5, cores=8, mem_total=16 << 30)
    d = json.loads(json.dumps(rep.to_dict()))
    assert set(d) == {"t", "cores", "mem_total", "frontmost", "totals", "on_screen",
                      "unseen_busy", "unseen_idle", "thresholds", "note"}
    assert d["totals"] == {"on_screen": 3, "unseen_busy": 3, "unseen_idle": 1 + len(IDLE)}
    assert d["frontmost"]["pid"] == BROWSER
    row = d["unseen_busy"][0]
    assert {"pid", "ppid", "name", "user", "cpu", "rss", "net_rate", "threads", "status",
            "kind", "why"} == set(row)


def test_markdown_has_three_sections_caps_idle_rows_and_escapes_pipes():
    procs = city() + [P(900, 1, "weird|name", cpu=50.0)]      # busy, so it is always listed
    rep = build_report(procs, {BROWSER}, BROWSER, t=0.0, cores=8, mem_total=16 << 30)
    md = render_markdown(rep, "2026-10-19 16:00:00")
    assert "# proccity report — 2026-10-19 16:00:00" in md
    for head in ("## On screen (3)", "## Unseen & busy (4)", f"## Unseen & idle ({1 + len(IDLE)})"):
        assert head in md
    assert "Frontmost: **firefox** (pid 100)" in md
    assert "| weird\\|name | 900 | user | 50% |" in md
    idle_section = md.split("## Unseen & idle")[1]
    hidden = 1 + len(IDLE) - IDLE_ROWS
    assert f"Top {IDLE_ROWS} by memory; {hidden} more not listed." in idle_section
    assert idle_section.count("\n| ") == IDLE_ROWS + 1   # header + capped rows
    assert md.rstrip().endswith("endpoint components.")


def test_empty_buckets_render_nothing_markers():
    md = render_markdown(build_report([], set(), None, t=0.0, cores=1, mem_total=1), "now")
    assert md.count("_nothing_") == 3


def test_byte_and_rate_formatting():
    assert fmt_bytes(0) == "0 B" and fmt_bytes(1023) == "1023 B"
    assert fmt_bytes(1536) == "1.5 KiB" and fmt_bytes(3 << 30) == "3.0 GiB"
    assert fmt_bytes(5000 << 30) == "5000.0 GiB"       # never overflows the unit list
    assert fmt_rate(50 * 1024) == "50.0 KiB/s"
