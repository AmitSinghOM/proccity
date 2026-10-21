"""Sort a snapshot into what is on screen and what is working unseen.

Pure functions only: no psutil, no subprocess, no clock, so every bucket rule is unit-testable
over synthetic snapshots. The server hands in the processes, the set of on-screen pids and the
machine facts; this module hands back a report.

Three buckets, deliberately neutral:

- **On screen** — the process, or an ancestor of it, owns a visible window. A browser's
  helpers are on screen because the browser is: the user is driving that work.
- **Unseen & busy** — no visible window, and it is burning CPU, moving bytes over the network
  or to disk, or sitting on a lot of memory right now. This is the list worth reading.
- **Unseen & idle** — no visible window and nothing much happening. Counted, not dwelt on.

The report says what is unseen. It does not say whether that is bad. On a managed Mac the
top unseen talkers are routinely the endpoint-security agent, the sync daemon and
mDNSResponder, all legitimate; a tool that called those "dangerous" would be wrong far more
often than right. So each row carries a *kind* — user, system or security — from a short list
of well-known names, and the prose stays descriptive.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .layout import ROOT_PPIDS, Proc

# "Busy" thresholds. Absolute, not percentile: a percentile would always label the top 10%
# busy even on a machine doing nothing, which is the opposite of honest.
BUSY_CPU = 5.0                 # percent of one core
BUSY_NET = 50 * 1024           # bytes per second, in + out
BUSY_RSS = 1 << 30             # 1 GiB resident
BUSY_DISK = 1 << 20            # 1 MiB/s to or from storage
IDLE_ROWS = 15                 # how many idle rows the Markdown report lists (by memory)

# Well-known unseen workers. Prefix match on the process name, case-insensitive. This is a
# label for the reader, not a verdict: a name on this list is still reported, just explained.
KNOWN_SYSTEM: tuple[str, ...] = (
    "launchd", "kernel_task", "mDNSResponder", "apsd", "nsurlsessiond", "cloudd", "bird",
    "rapportd", "trustd", "nehelper", "identityservicesd", "mds", "mdworker", "mds_stores",
    "backupd", "softwareupdated", "airportd", "configd", "symptomsd", "akd", "sharingd",
    "remindd", "dasd", "coreduetd", "biomed", "cloudphotod", "photolibraryd", "CalendarAgent",
    "appstoreagent", "timed", "netbiosd", "WindowServer", "loginwindow", "Dock", "Finder",
    "SystemUIServer", "ControlCenter", "Spotlight", "corespotlightd", "searchpartyd",
    "mediaanalysisd", "photoanalysisd", "suggestd", "knowledge-agent", "contextstored",
    "com.apple.", "UserEventAgent", "secd", "securityd", "opendirectoryd", "syspolicyd",
    "locationd", "assistantd", "siriknowledged", "distnoted", "cfprefsd", "logd", "notifyd",
    "powerd", "diskarbitrationd", "coreaudiod", "bluetoothd", "wifid", "usbd", "systemd",
    "dbus-daemon", "NetworkManager", "pulseaudio", "pipewire", "Xorg", "gnome-shell",
)
KNOWN_SECURITY: tuple[str, ...] = (
    "com.crowdstrike", "falcon", "TaniumClient", "Tanium", "SentinelAgent", "sentineld",
    "CylanceSvc", "MsMpEng", "wdavdaemon", "Microsoft Defender", "jamf", "JamfDaemon",
    "kandji", "Kolide", "launcher", "osqueryd", "zscaler", "ZscalerTunnel", "GlobalProtect",
    "PanGPS", "vpnagentd", "Cisco", "LuLu", "Little Snitch", "ACME", "ESET", "Sophos",
)


@dataclass(frozen=True)
class Row:
    pid: int
    ppid: int
    name: str
    user: str
    cpu: float          # percent of one core
    rss: int            # bytes
    net_rate: float     # bytes per second, in + out
    threads: int
    status: str
    kind: str           # user | system | security
    why: tuple[str, ...] = ()   # what made it "busy"; empty for idle and on-screen rows
    identity: str = "unknown"   # verified | unverified | unknown
    signer: str = ""            # who signed it, when verified; what is wrong, when not
    files: int | None = None    # open files, or None when the OS would not let us look
    sockets: int | None = None  # inet sockets, or None
    children: int = 0
    disk_rate: float | None = None      # bytes/s to+from storage; None = not measured
    threads_active: int | None = None   # threads whose CPU moved; None = not measured
    top_thread_share: float | None = None  # 0..1 of that movement in the busiest thread


@dataclass(frozen=True)
class Sources:
    """How each signal was obtained, so a reader can tell 'measured 0' from 'not measured'."""
    screen: str = "unknown"     # quartz | frontmost-only | win32 | x11 | unknown
    net: str = "needs-admin"    # nettop | needs-admin | unavailable
    identity: str = "unknown"   # codesign | unknown
    disk: str = "unavailable"   # rusage | psutil | unavailable
    threads: str = "unavailable"  # libproc | psutil | unavailable


@dataclass
class Report:
    t: float
    cores: int
    mem_total: int
    frontmost: Row | None
    sources: Sources = field(default_factory=Sources)
    on_screen: list[Row] = field(default_factory=list)
    unseen_busy: list[Row] = field(default_factory=list)
    unseen_idle: list[Row] = field(default_factory=list)

    @property
    def note(self) -> str:
        return ("This report shows what is running unseen. It does not judge it. Rows marked "
                "system or security are well-known macOS or endpoint components. Identity "
                "says who signed the running code, never whether it is safe.")

    def to_dict(self) -> dict:
        return {
            "t": self.t, "cores": self.cores, "mem_total": self.mem_total,
            "frontmost": asdict(self.frontmost) if self.frontmost else None,
            "sources": asdict(self.sources),
            "totals": {"on_screen": len(self.on_screen), "unseen_busy": len(self.unseen_busy),
                       "unseen_idle": len(self.unseen_idle)},
            "on_screen": [asdict(r) for r in self.on_screen],
            "unseen_busy": [asdict(r) for r in self.unseen_busy],
            "unseen_idle": [asdict(r) for r in self.unseen_idle],
            "thresholds": {"cpu": BUSY_CPU, "net": BUSY_NET, "rss": BUSY_RSS, "disk": BUSY_DISK},
            "note": self.note,
        }


def kind_of(name: str) -> str:
    low = name.lower()
    if any(low.startswith(k.lower()) for k in KNOWN_SECURITY):
        return "security"
    if any(low.startswith(k.lower()) for k in KNOWN_SYSTEM):
        return "system"
    return "user"


def expand_to_families(procs: list[Proc], screen_pids: set[int]) -> set[int]:
    """A pid is on screen if it, or any ancestor, owns a visible window.

    Walks each process up to its root; guards against cycles and missing parents the same way
    layout.root_of does. The result includes the ancestors themselves.
    """
    if not screen_pids:
        return set()
    ppid_of = {p.pid: p.ppid for p in procs}
    out: set[int] = set()
    for p in procs:
        pid, seen = p.pid, set()
        while pid not in seen:
            seen.add(pid)
            if pid in screen_pids:
                out.update(seen)
                break
            parent = ppid_of.get(pid)
            if parent is None or parent in ROOT_PPIDS or parent == pid or parent not in ppid_of:
                break
            pid = parent
    return out


def busy_reasons(p: Proc) -> tuple[str, ...]:
    why: list[str] = []
    if p.cpu >= BUSY_CPU:
        why.append(f"cpu {p.cpu:.0f}%")
    if p.net_rate >= BUSY_NET:
        why.append(f"net {fmt_rate(p.net_rate)}")
    if p.rss >= BUSY_RSS:
        why.append(f"mem {fmt_bytes(p.rss)}")
    if p.disk_rate is not None and p.disk_rate >= BUSY_DISK:
        why.append(f"disk {fmt_rate(p.disk_rate)}")
    return tuple(why)


def _row(p: Proc, why: tuple[str, ...] = ()) -> Row:
    return Row(pid=p.pid, ppid=p.ppid, name=p.name, user=p.user, cpu=round(p.cpu, 1),
               rss=p.rss, net_rate=round(p.net_rate, 1), threads=p.threads, status=p.status,
               kind=kind_of(p.name), why=why, identity=p.identity, signer=p.signer,
               files=p.files, sockets=p.sockets, children=p.children,
               disk_rate=None if p.disk_rate is None else round(p.disk_rate, 1),
               threads_active=p.threads_active,
               top_thread_share=(None if p.top_thread_share is None
                                 else round(p.top_thread_share, 3)))


def _activity(r: Row) -> float:
    # one number to sort by: a core of CPU, a MiB/s of network or disk and a GiB of memory
    # each count about the same, so no single axis hides the others
    return (r.cpu / 100 + r.net_rate / (1 << 20) + r.rss / (1 << 30)
            + (r.disk_rate or 0.0) / (1 << 20))


def build_report(procs: list[Proc], screen_pids: set[int], frontmost_pid: int | None,
                 t: float, cores: int, mem_total: int,
                 sources: Sources | None = None) -> Report:
    on_screen_pids = expand_to_families(procs, screen_pids)
    rep = Report(t=t, cores=cores, mem_total=mem_total, frontmost=None,
                 sources=sources or Sources())
    for p in procs:
        if p.pid in on_screen_pids:
            rep.on_screen.append(_row(p))
        else:
            why = busy_reasons(p)
            (rep.unseen_busy if why else rep.unseen_idle).append(_row(p, why))
    for bucket in (rep.on_screen, rep.unseen_busy, rep.unseen_idle):
        bucket.sort(key=lambda r: (-_activity(r), r.pid))
    if frontmost_pid is not None:
        rep.frontmost = next((r for r in rep.on_screen if r.pid == frontmost_pid), None)
    return rep


# ---- rendering ---------------------------------------------------------------------------

def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"


def fmt_rate(bps: float) -> str:
    return fmt_bytes(bps) + "/s"


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _net_cell(r: Row, net_source: str) -> str:
    if net_source == "needs-admin":
        return "needs admin"
    if net_source == "unavailable":
        return "n/a"
    return fmt_rate(r.net_rate)


def _id_cell(r: Row) -> str:
    if r.identity in ("verified", "unverified"):
        return f"{r.identity} ({_md_escape(r.signer)})" if r.signer else r.identity
    return "unknown"


def _n(v: int | None) -> str:
    return "–" if v is None else str(v)


def _disk_cell(r: Row) -> str:
    return "–" if r.disk_rate is None else fmt_rate(r.disk_rate)


def _threads_cell(r: Row) -> str:
    """`12 (3 hot, 71%)`: thread count, how many moved last tick, share of the busiest."""
    if r.threads_active is None or r.top_thread_share is None:
        return str(r.threads)
    if r.threads_active == 0:
        return f"{r.threads} (idle)"
    return f"{r.threads} ({r.threads_active} hot, {r.top_thread_share * 100:.0f}%)"


def _table(rows: list[Row], with_why: bool, net_source: str) -> list[str]:
    head = ("| process | pid | kind | identity | cpu | mem | net | disk | threads | files | "
            "sockets | children |")
    rule = "|---|---:|---|---|---:|---:|---:|---:|---|---:|---:|---:|"
    if with_why:
        head += " why |"
        rule += "---|"
    out = [head, rule]
    for r in rows:
        line = (f"| {_md_escape(r.name)} | {r.pid} | {r.kind} | {_id_cell(r)} | {r.cpu:.0f}% | "
                f"{fmt_bytes(r.rss)} | {_net_cell(r, net_source)} | {_disk_cell(r)} | "
                f"{_threads_cell(r)} | {_n(r.files)} | {_n(r.sockets)} | {r.children} |")
        if with_why:
            line += f" {', '.join(r.why)} |"
        out.append(line)
    return out


_SCREEN_TEXT = {
    "quartz": "On-screen set: every visible window (Quartz).",
    "frontmost-only": "On-screen set: the frontmost app only (install `proccity[mac]` for "
                      "every window).",
    "win32": "On-screen set: every visible top-level window (Win32).",
    "x11": "On-screen set: every mapped X11 client window.",
    "unknown": "On-screen set: **unknown** on this session (Wayland or headless), so every "
               "process is listed as unseen.",
}
_NET_TEXT = {
    "nettop": "Network: per-process rate from nettop.",
    "needs-admin": "Network: **not measured** -- this platform has no per-process counter "
                   "without admin rights.",
    "unavailable": "Network: nettop failed on this host; rates not available.",
}
_ID_TEXT = {
    "codesign": "Identity: signature of the running code, checked a few processes per sample; "
                "says who signed it, never whether it is safe.",
    "unknown": "Identity: not checked on this platform.",
}
_DISK_TEXT = {
    "rusage": "Disk: bytes to and from storage per second, from the kernel's per-process "
              "accounting (libproc); your own processes only, a dash for the rest.",
    "psutil": "Disk: bytes to and from storage per second, from the OS per-process counters; "
              "your own processes only, a dash for the rest.",
    "unavailable": "Disk: **not measured** on this platform.",
}
_THREADS_TEXT = {
    "libproc": "Threads: count, how many used CPU since the last sample, and the share of "
               "that in the busiest one (libproc); your own processes only.",
    "psutil": "Threads: count, how many used CPU since the last sample, and the share of "
              "that in the busiest one; your own processes only.",
    "unavailable": "Threads: count only; the per-thread split is **not measured** here.",
}


def render_markdown(rep: Report, when: str) -> str:
    src = rep.sources
    total = len(rep.on_screen) + len(rep.unseen_busy) + len(rep.unseen_idle)
    lines = [f"# proccity report — {when}", "",
             f"{rep.cores} cores, {fmt_bytes(rep.mem_total)} memory, {total} processes."]
    if rep.frontmost:
        lines.append(f"Frontmost: **{_md_escape(rep.frontmost.name)}** (pid {rep.frontmost.pid})")
    lines += ["", "What this report could measure here:", "",
              f"- {_SCREEN_TEXT.get(src.screen, _SCREEN_TEXT['unknown'])}",
              f"- {_NET_TEXT.get(src.net, _NET_TEXT['needs-admin'])}",
              f"- {_DISK_TEXT.get(src.disk, _DISK_TEXT['unavailable'])}",
              f"- {_THREADS_TEXT.get(src.threads, _THREADS_TEXT['unavailable'])}",
              f"- {_ID_TEXT.get(src.identity, _ID_TEXT['unknown'])}",
              "- Files, sockets, children are counts of what the process holds right now; "
              "a dash means the OS would not let this user look."]
    lines += ["", f"## On screen ({len(rep.on_screen)})", ""]
    lines += _table(rep.on_screen, False, src.net) if rep.on_screen else ["_nothing_"]
    lines += ["", f"## Unseen & busy ({len(rep.unseen_busy)})", "",
              f"Busy means cpu ≥ {BUSY_CPU:.0f}%, net ≥ {fmt_rate(BUSY_NET)}, "
              f"disk ≥ {fmt_rate(BUSY_DISK)} or mem ≥ {fmt_bytes(BUSY_RSS)}.", ""]
    lines += _table(rep.unseen_busy, True, src.net) if rep.unseen_busy else ["_nothing_"]
    idle = rep.unseen_idle
    lines += ["", f"## Unseen & idle ({len(idle)})", ""]
    if idle:
        shown = sorted(idle, key=lambda r: -r.rss)[:IDLE_ROWS]
        lines.append(f"Top {len(shown)} by memory; {len(idle) - len(shown)} more not listed.")
        lines.append("")
        lines += _table(shown, False, src.net)
    else:
        lines.append("_nothing_")
    lines += ["", "---", "", rep.note, ""]
    return "\n".join(lines)
