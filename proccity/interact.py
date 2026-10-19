"""How much a process holds right now: open files, inet sockets, children. Counts only.

This is the static half of "what is this process interacting with". The live half -- which
file it touched when, which peer it talked to -- is tracing (strace, fs_usage, ETW) and is
root on every OS, so proccity does not do it.

Counts are deliberately the whole story. The report's privacy contract shows no paths and no
addresses; a number of files and a number of sockets says "busy with I/O" without naming a
single thing. psutil can give the paths and peers; this module throws them away before they
leave the function.

Access: a user can count their own processes' files and sockets on every OS. Other users'
processes (root daemons, other logins) deny the read on macOS and Linux; those report None,
rendered as a dash, never as 0 -- zero would be a claim we did not measure. Children come
from the parent-pid table, which is free and never denied. Measured: ~0.2 s for ~500 own
processes on macOS, so this runs every tick.
"""
from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class Counts:
    files: int | None       # open regular files; None = not permitted to look
    sockets: int | None     # inet sockets (tcp+udp, v4+v6); None = not permitted
    children: int           # direct children, from the ppid table


def children_table(ppids: dict[int, int]) -> dict[int, int]:
    """{pid: ppid} -> {ppid: number of direct children}. Pure."""
    out: dict[int, int] = {}
    for ppid in ppids.values():
        out[ppid] = out.get(ppid, 0) + 1
    return out


def count_one(pid: int) -> tuple[int | None, int | None]:
    """(files, sockets) for one pid; (None, None) when the OS refuses or the pid is gone."""
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            files = len(p.open_files())
            sockets = len(p.net_connections(kind="inet"))
        return files, sockets
    except (psutil.AccessDenied, psutil.ZombieProcess, psutil.NoSuchProcess):
        return None, None
    except OSError:
        return None, None


def count_all(ppids: dict[int, int], enabled: bool = True) -> dict[int, Counts]:
    kids = children_table(ppids)
    out: dict[int, Counts] = {}
    for pid in ppids:
        files, sockets = count_one(pid) if enabled else (None, None)
        out[pid] = Counts(files=files, sockets=sockets, children=kids.get(pid, 0))
    return out
