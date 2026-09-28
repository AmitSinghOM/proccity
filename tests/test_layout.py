from proccity.layout import (
    LOT,
    CityState,
    Proc,
    height_for,
    layout,
    root_of,
    spiral,
)


def P(pid, ppid, rss=50 << 20, threads=4, cpu=0.0, name="p", user="u"):
    return Proc(pid=pid, ppid=ppid, name=name, user=user, cpu=cpu, rss=rss,
                threads=threads, status="running")


def test_spiral_cells_are_unique_for_first_200():
    cells = [spiral(n) for n in range(200)]
    assert len(set(cells)) == 200
    assert cells[0] == (0, 0)


def test_root_of_walks_to_top_level_and_survives_cycles():
    ppid = {1: 0, 10: 1, 20: 10, 30: 20}
    assert root_of(30, ppid) == 10
    ppid_cycle = {5: 6, 6: 5}
    assert root_of(5, ppid_cycle) in (5, 6)  # terminates
    assert root_of(99, {}) == 99             # unknown parent -> itself


def test_related_processes_share_a_district_and_unrelated_do_not():
    procs = [P(10, 1), P(11, 10), P(12, 11), P(20, 1), P(21, 20)]
    out = layout(procs, CityState())
    d = {b.pid: b.district for b in out}
    assert d[10] == d[11] == d[12]
    assert d[20] == d[21]
    assert d[10] != d[20]


def test_no_two_buildings_share_a_lot():
    procs = [P(10, 1)] + [P(100 + i, 10) for i in range(40)] + [P(20, 1), P(21, 20)]
    out = layout(procs, CityState())
    coords = [(round(b.x, 3), round(b.z, 3)) for b in out]
    assert len(coords) == len(set(coords)) == len(procs)


def test_positions_are_stable_across_snapshots_and_lots_are_reused():
    state = CityState()
    first = {b.pid: (b.x, b.z) for b in layout([P(10, 1), P(11, 10), P(12, 10)], state)}
    # 11 dies, 13 is born, 12 changes memory: 10 and 12 must not move
    procs2 = [P(10, 1), P(12, 10, rss=900 << 20), P(13, 10)]
    second = {b.pid: (b.x, b.z) for b in layout(procs2, state)}
    assert second[10] == first[10]
    assert second[12] == first[12]
    assert second[13] == first[11]   # released lot is handed to the newcomer
    assert 11 not in second


def test_height_is_monotonic_in_rss_and_log_scaled():
    h1, h10, h100 = height_for(10 << 20), height_for(100 << 20), height_for(1000 << 20)
    assert h1 < h10 < h100
    assert (h100 - h10) < 2 * (h10 - h1) + 1e-9   # each 10x adds roughly the same, not 10x more


def test_districts_do_not_overlap_even_when_full():
    procs = ([P(10, 1)] + [P(100 + i, 10) for i in range(63)]
             + [P(20, 1)] + [P(300 + i, 20) for i in range(63)])
    out = layout(procs, CityState())
    a = [b for b in out if b.district == 0]
    b_ = [b for b in out if b.district == 1]
    assert len(a) == 64 and len(b_) == 64
    ax = (min(x.x for x in a), max(x.x for x in a))
    bx = (min(x.x for x in b_), max(x.x for x in b_))
    az = (min(x.z for x in a), max(x.z for x in a))
    bz = (min(x.z for x in b_), max(x.z for x in b_))
    x_sep = ax[1] + LOT <= bx[0] or bx[1] + LOT <= ax[0]
    z_sep = az[1] + LOT <= bz[0] or bz[1] + LOT <= az[0]
    assert x_sep or z_sep


def test_loners_are_pooled_into_commons_and_families_get_their_own_district():
    procs = [P(100 + i, 1) for i in range(10)] + [P(50, 1), P(51, 50)]
    state = CityState()
    out = layout(procs, state)
    d = {b.pid: b.district for b in out}
    assert len({d[100 + i] for i in range(10)}) == 1          # all loners share one commons
    assert d[50] == d[51] and d[50] != d[100]                  # the family has its own block
    assert state.commons == [d[100]]


def test_commons_overflow_opens_a_second_commons():
    procs = [P(1000 + i, 1) for i in range(70)]
    state = CityState()
    out = layout(procs, state)
    assert len(out) == 70
    assert len(state.commons) == 2


def test_dead_family_district_is_released_and_its_block_reused():
    """Staff R1: over hours, dead families must not leave empty blocks forever."""
    state = CityState()
    layout([P(10, 1), P(11, 10)], state)                    # family -> district 0
    d0 = state.district_of_root[10]
    layout([P(10, 1), P(11, 10), P(99, 1), P(98, 99)], state)   # a second family arrives
    assert state.district_of_root[99] != d0
    layout([P(99, 1), P(98, 99)], state)                    # family 10 is gone now
    assert 10 not in state.district_of_root
    assert d0 not in state.district_origin                  # block released
    out = layout([P(99, 1), P(98, 99), P(200, 1), P(201, 200)], state)
    assert {b.district for b in out if b.pid in (200, 201)} == {d0}  # block reused


def test_orphaned_child_keeps_its_lot_and_takes_no_second_one():
    """Staff R2: when a parent dies the child is re-parented and becomes a root; it must
    keep its existing lot, not also be handed a fresh commons lot that then leaks."""
    state = CityState()
    first = {b.pid: (b.x, b.z) for b in layout([P(10, 1), P(11, 10)], state)}
    used_before = sum(len(s) for s in state.lots_used.values())
    second = {b.pid: (b.x, b.z) for b in layout([P(11, 1)], state)}
    assert second[11] == first[11]
    used_after = sum(len(s) for s in state.lots_used.values())
    assert used_after == 1, (used_before, used_after)
