"""proccity: your running processes as a living city.

Run `python -m proccity` and open http://127.0.0.1:8765/.

Threat model, in three lines. The snapshot exposes process names, users and memory for this
machine, so the server binds loopback only and has no auth: anything that can reach it is
already on your machine. The one remote attacker that can still reach a loopback service is
a web page you visit, via DNS rebinding (attacker.example resolving to 127.0.0.1 so the
browser treats it as same-origin). Requests are therefore refused unless the Host header
names loopback, which is the standard mitigation.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

from .diskio import DiskSampler
from .focus import FocusSampler
from .identity import IdentityChecker
from .interact import count_all
from .layout import CityState, Proc, layout
from .netio import NetSampler
from .report import Report, Sources, build_report, expand_to_families, render_markdown
from .threads import ThreadSampler, ThreadSplit

log = logging.getLogger("proccity")
STATIC = Path(__file__).parent / "static"
# Allowlist, not a directory walk: the only files this server will ever serve.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
# Generated responses, same allowlist idea: path -> (content type, producer on the City).
API = {
    "/api/snapshot": ("application/json", lambda c: c.snapshot_bytes()),
    "/report.json": ("application/json", lambda c: c.report_json_bytes()),
    "/report.md": ("text/markdown; charset=utf-8", lambda c: c.report_markdown_bytes()),
}


def sample() -> list[Proc]:
    procs: list[Proc] = []
    for p in psutil.process_iter(["pid", "ppid", "name", "username", "memory_info",
                                  "num_threads", "status", "create_time"]):
        info = p.info
        try:
            cpu = p.cpu_percent(interval=None)  # since last call; first call is 0.0
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        mem = info.get("memory_info")
        procs.append(Proc(
            pid=info["pid"], ppid=info.get("ppid") or 0,
            name=info.get("name") or "?", user=info.get("username") or "?",
            cpu=cpu, rss=getattr(mem, "rss", 0) if mem else 0,
            threads=info.get("num_threads") or 1, status=info.get("status") or "?",
            create_time=info.get("create_time") or 0.0,
        ))
    return procs


def enrich(procs: list[Proc], net_rates: dict[int, float], screen_pids: set[int],
           identities: dict | None = None, counts: dict | None = None,
           disk_rates: dict[int, float] | None = None,
           splits: dict[int, ThreadSplit] | None = None) -> list[Proc]:
    """Pure: stamp each Proc with its network rate, on-screen family membership, identity
    verdict, interaction counts, disk rate and thread split. Missing maps leave the defaults
    in place; a pid missing from disk_rates or splits stays None ("not measured")."""
    visible = expand_to_families(procs, screen_pids)
    identities = identities or {}
    counts = counts or {}
    disk_rates = disk_rates or {}
    splits = splits or {}
    out = []
    for p in procs:
        fields: dict = {"net_rate": net_rates.get(p.pid, 0.0), "on_screen": p.pid in visible}
        if ident := identities.get(p.pid):
            fields.update(identity=ident.state, signer=ident.signer)
        if c := counts.get(p.pid):
            fields.update(files=c.files, sockets=c.sockets, children=c.children)
        if p.pid in disk_rates:
            fields["disk_rate"] = disk_rates[p.pid]
        if s := splits.get(p.pid):
            fields.update(threads_active=s.active, top_thread_share=s.top_share)
        out.append(replace(p, **fields))
    return out


class City:
    """Samples on a background thread so HTTP requests never block on psutil."""

    def __init__(self, interval: float, start: bool = True, net: NetSampler | None = None,
                 focus: FocusSampler | None = None, identity: IdentityChecker | None = None,
                 disk: DiskSampler | None = None, threads: ThreadSampler | None = None) -> None:
        self.interval = interval
        self.state = CityState()
        self.net = net if net is not None else NetSampler()
        self.focus = focus if focus is not None else FocusSampler()
        self.identity = identity if identity is not None else IdentityChecker()
        self.disk = disk if disk is not None else DiskSampler()
        self.threads = threads if threads is not None else ThreadSampler()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._snapshot: dict = {"t": 0.0, "interval": interval, "cores": psutil.cpu_count() or 1,
                                "mem_total": 0, "mem_used": 0, "frontmost": None,
                                "sources": asdict(self._sources()), "buildings": []}
        self._report: Report | None = None
        if start:
            self.tick()  # first paint immediately; cpu reads 0 until the second sample
            threading.Thread(target=self._loop, daemon=True, name="proccity-sampler").start()

    def _sources(self) -> Sources:
        return Sources(screen=self.focus.source, net=self.net.source,
                       identity=self.identity.source, disk=self.disk.source,
                       threads=self.threads.source)

    def tick(self) -> None:
        procs = sample()
        net_rates = self.net.sample()
        pids = [p.pid for p in procs]
        disk_rates = self.disk.sample(pids)
        splits = self.threads.sample(pids)
        screen_pids = self.focus.screen_pids()
        frontmost = self.focus.frontmost_pid() if screen_pids else None
        visible = expand_to_families(procs, screen_pids)
        # Identity is budgeted per tick: check the unseen and busy first, the user's own
        # visible apps next, then everything else.
        def priority(p: Proc) -> tuple:
            busy = (p.cpu >= 5 or net_rates.get(p.pid, 0.0) >= 50 * 1024 or p.rss >= (1 << 30)
                    or disk_rates.get(p.pid, 0.0) >= (1 << 20))
            return (0 if (busy and p.pid not in visible) else 1 if p.pid in visible else 2,
                    -p.cpu, p.pid)
        identities = self.identity.refresh(procs, priority)
        counts = count_all({p.pid: p.ppid for p in procs})
        procs = enrich(procs, net_rates, screen_pids, identities, counts, disk_rates, splits)
        buildings = layout(procs, self.state)
        vm = psutil.virtual_memory()
        now = round(time.time(), 3)
        cores = psutil.cpu_count() or 1
        sources = self._sources()
        snap = {
            "t": now,
            "interval": self.interval,
            "cores": cores,
            # real machine numbers for the HUD: summing RSS double-counts shared pages and
            # can exceed physical RAM, which makes "rent" a lie
            "mem_total": vm.total,
            "mem_used": vm.total - vm.available,
            "frontmost": frontmost,
            "sources": asdict(sources),
            "buildings": [b.__dict__ for b in buildings],
        }
        report = build_report(procs, screen_pids, frontmost, t=now, cores=cores,
                              mem_total=vm.total, sources=sources)
        with self._lock:
            self._snapshot = snap
            self._report = report

    def _safe_tick(self) -> None:
        try:
            self.tick()
        except Exception:  # a sampler hiccup must not kill the only sampler thread
            log.exception("sample failed; keeping last snapshot")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._safe_tick()

    def stop(self) -> None:
        self._stop.set()

    def snapshot_bytes(self) -> bytes:
        with self._lock:
            snap = self._snapshot
        return json.dumps(snap, separators=(",", ":")).encode()

    def report_json_bytes(self) -> bytes:
        with self._lock:
            rep = self._report
        body = rep.to_dict() if rep else {"error": "no sample yet"}
        return json.dumps(body, separators=(",", ":")).encode()

    def report_markdown_bytes(self) -> bytes:
        with self._lock:
            rep = self._report
        if rep is None:
            return b"# proccity report\n\nno sample yet\n"
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rep.t))
        return render_markdown(rep, when).encode()


def host_is_loopback(host_header: str | None) -> bool:
    """True only when the Host header names this machine's loopback interface.

    A page at attacker.example whose DNS answer is 127.0.0.1 arrives with
    `Host: attacker.example`; refusing it defeats DNS rebinding.
    """
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):                     # [::1]:8765
        host = host.split("]")[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host in LOOPBACK_HOSTS


def make_handler(city: City):
    class Handler(BaseHTTPRequestHandler):
        server_version = "proccity"
        sys_version = ""
        # Every response carries Content-Length, so keep-alive is safe and the browser stops
        # opening a fresh TCP connection for each 2-second poll.
        protocol_version = "HTTP/1.1"
        # http.server's default error page is HTML that names Python; keep it plain.
        error_message_format = "%(code)d %(message)s\n"
        error_content_type = "text/plain; charset=utf-8"

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            self._serve(head=False)

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(head=True)

        def _method_not_allowed(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self._finish(b"method not allowed\n", "text/plain")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed  # noqa: N815

        def _serve(self, head: bool) -> None:
            if not host_is_loopback(self.headers.get("Host")):
                self._send(421, "text/plain", b"proccity only answers to loopback hosts\n", head)
                return
            path = self.path.split("?", 1)[0]
            if path in API:
                ctype, producer = API[path]
                self._send(200, ctype, producer(city), head)
            elif path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                self._send(200, ctype, (STATIC / name).read_bytes(), head)
            else:
                self._send(404, "text/plain", b"not found\n", head)

        def _send(self, code: int, ctype: str, body: bytes, head: bool = False) -> None:
            self.send_response(code)
            self._finish(body, ctype, head)

        def _finish(self, body: bytes, ctype: str, head: bool = False) -> None:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            log.debug(fmt, *args)

    return Handler


def positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def port_number(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("must be 1..65535")
    return value


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="proccity", description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=port_number, default=8765)
    ap.add_argument("--interval", type=positive_float, default=2.0,
                    help="sampling period in seconds (default 2)")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    city = City(args.interval)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(city))
    srv.daemon_threads = True
    print(f"proccity: http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        city.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
