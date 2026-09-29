"""Turn a process snapshot into city geometry.

Pure functions only: no psutil, no I/O, so the layout is unit-testable and the server is a
thin shell around it. The rules, chosen so the picture means something:

- A *district* is a top-level process (parent is PID 0/1/launchd) plus every descendant.
  Related work sits together: a browser and its helpers, a shell and what it spawned.
- Districts are placed on an outward spiral in first-seen order and never move. Buildings
  inside a district fill a square grid in first-seen order and never move either. New
  processes take the next free lot; a dead process's lot is released. A city you can watch
  needs stable streets.
- Building height is log-scaled resident memory (RSS), so a 10x memory hog is visibly but
  not absurdly taller. Footprint is thread count. CPU lights the windows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

ROOT_PPIDS = frozenset({0, 1})
LOT = 1.6          # world units per building lot (building is ~1.0 wide, rest is street)
DISTRICT_GAP = 3.0  # street between districts
DISTRICT_SIDE = 8   # lots per side; 64 lots per district
MIN_HEIGHT = 0.4
HEIGHT_PER_LOG2_MB = 0.9


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    name: str
    user: str
    cpu: float          # percent, 0..(100*cores)
    rss: int            # bytes
    threads: int
    status: str


@dataclass
class Building:
    pid: int
    name: str
    user: str
    district: int
    x: float
    z: float
    width: float
    height: float
    cpu: float
    rss: int
    threads: int
    status: str


@dataclass
class CityState:
    """Everything the layout needs to remember between snapshots to stay stable."""
    district_of_root: dict[int, int] = field(default_factory=dict)   # root pid -> district id
    district_origin: dict[int, tuple[float, float]] = field(default_factory=dict)
    lot_of_pid: dict[int, tuple[int, int]] = field(default_factory=dict)  # pid -> (district, lot)
    lots_used: dict[int, set[int]] = field(default_factory=dict)      # district -> used lot idx
    commons: list[int] = field(default_factory=list)                  # districts pooling loners
    free_districts: list[int] = field(default_factory=list)           # released ids, reused first
    next_district: int = 0

    def new_district(self) -> int:
        # Reuse a released block before spiralling outward, so a long-running city does not
        # creep away from the origin as families come and go.
        d = self.free_districts.pop(0) if self.free_districts else self._fresh_district()
        gx, gz = spiral(d)
        pitch = DISTRICT_SIDE * LOT + DISTRICT_GAP   # fixed pitch: districts can never collide
        self.district_origin[d] = (gx * pitch, gz * pitch)
        self.lots_used[d] = set()
        return d

    def _fresh_district(self) -> int:
        d = self.next_district
        self.next_district += 1
        return d

    def commons_with_room(self) -> int:
        for d in self.commons:
            if len(self.lots_used[d]) < DISTRICT_SIDE * DISTRICT_SIDE:
                return d
        d = self.new_district()
        self.commons.append(d)
        return d

    def release_empty_districts(self) -> None:
        """Drop family districts with no buildings left. Commons blocks are kept: they are
        shared, and keeping them is what makes loner positions stable."""
        for d in [d for d, used in self.lots_used.items() if not used and d not in self.commons]:
            del self.lots_used[d]
            del self.district_origin[d]
            self.free_districts.append(d)
            for root in [r for r, dd in self.district_of_root.items() if dd == d]:
                del self.district_of_root[root]
        self.free_districts.sort()


def root_of(pid: int, ppid_of: dict[int, int]) -> int:
    """Walk up to the top-level ancestor. Guards against cycles and missing parents."""
    seen = set()
    while pid not in seen:
        seen.add(pid)
        parent = ppid_of.get(pid)
        if parent is None or parent in ROOT_PPIDS or parent == pid or parent not in ppid_of:
            return pid
        pid = parent
    return pid


def spiral(n: int) -> tuple[int, int]:
    """n-th cell of an outward square spiral from (0,0). Deterministic, no overlap."""
    if n == 0:
        return (0, 0)
    k = math.ceil((math.sqrt(n + 1) - 1) / 2)   # ring index
    t = 2 * k + 1
    m = t * t
    t -= 1
    if n >= m - t:
        return (k - (m - n), -k)
    m -= t
    if n >= m - t:
        return (-k, -k + (m - n))
    m -= t
    if n >= m - t:
        return (-k + (m - n), k)
    return (k, k - (m - n - t))


def height_for(rss: int) -> float:
    mb = max(rss, 1) / (1024 * 1024)
    return MIN_HEIGHT + HEIGHT_PER_LOG2_MB * math.log2(1 + mb)


def width_for(threads: int) -> float:
    return min(1.0, 0.45 + 0.08 * math.sqrt(max(threads, 1)))




def layout(procs: list[Proc], state: CityState) -> list[Building]:
    ppid_of = {p.pid: p.ppid for p in procs}
    alive = {p.pid for p in procs}

    # release lots of dead processes, then any family block that emptied out
    for pid in [pid for pid in state.lot_of_pid if pid not in alive]:
        district, lot = state.lot_of_pid.pop(pid)
        state.lots_used.get(district, set()).discard(lot)
    # A dead root's bookkeeping goes too. Commons blocks are never released, so without this
    # every short-lived loner (each `ls`, each cron child) would leave an entry behind forever,
    # and a recycled pid would inherit a stranger's district.
    for root in [r for r in state.district_of_root if r not in alive]:
        del state.district_of_root[root]
    state.release_empty_districts()

    # group by top-level ancestor, stable first-seen order
    by_root: dict[int, list[Proc]] = {}
    for p in sorted(procs, key=lambda p: p.pid):
        by_root.setdefault(root_of(p.pid, ppid_of), []).append(p)

    # A family (root + children) gets its own district. A loner (a top-level process with no
    # children; most of a macOS/systemd process table) is pooled into a shared commons
    # district so the city is dense instead of one tower per empty block. A loner that later
    # spawns children keeps its lot; the children join it in the commons.
    side = DISTRICT_SIDE

    def take_lot(d: int) -> int | None:
        used = state.lots_used[d]
        lot = next((i for i in range(side * side) if i not in used), None)
        if lot is not None:
            used.add(lot)
        return lot

    for root, members in by_root.items():
        if root in state.district_of_root:
            continue
        if root in state.lot_of_pid:
            # An orphan: its parent died and the OS re-parented it, so it is a root now. It
            # already owns a lot; its district is wherever that lot is.
            state.district_of_root[root] = state.lot_of_pid[root][0]
        elif len(members) > 1:
            state.district_of_root[root] = state.new_district()
        else:
            d = state.commons_with_room()          # has room by construction
            state.district_of_root[root] = d
            state.lot_of_pid[root] = (d, take_lot(d))

    out: list[Building] = []
    for root, members in by_root.items():
        d = state.district_of_root[root]
        for p in members:
            if p.pid not in state.lot_of_pid:
                lot = take_lot(d)
                if lot is None:
                    # District full (a browser with 70 helpers). Never drop a process from
                    # the picture: it moves to the commons and loses its family colour, which
                    # is honest and visible, where a missing building is neither.
                    d_spill = state.commons_with_room()
                    lot = take_lot(d_spill)
                    state.lot_of_pid[p.pid] = (d_spill, lot)
                else:
                    state.lot_of_pid[p.pid] = (d, lot)
            d_actual, lot = state.lot_of_pid[p.pid]
            ox, oz = state.district_origin[d_actual]
            lx, lz = lot % side, lot // side
            out.append(Building(
                pid=p.pid, name=p.name, user=p.user, district=d_actual,
                x=round(ox + lx * LOT, 3), z=round(oz + lz * LOT, 3),
                width=round(width_for(p.threads), 3), height=round(height_for(p.rss), 3),
                cpu=round(p.cpu, 1), rss=p.rss, threads=p.threads, status=p.status,
            ))
    return out
