"""proccity: your running processes as a living city.

Run `python -m proccity` and open http://127.0.0.1:8765/. Loopback only, no auth: the
snapshot exposes process names, users and memory for this machine, which is fine on your
own laptop and not something to put on a network.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

from .layout import CityState, Proc, layout

STATIC = Path(__file__).parent / "static"


def sample() -> list[Proc]:
    procs: list[Proc] = []
    for p in psutil.process_iter(["pid", "ppid", "name", "username", "memory_info",
                                  "num_threads", "status"]):
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
        ))
    return procs


class City:
    """Samples on a background thread so HTTP requests never block on psutil."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.state = CityState()
        self._lock = threading.Lock()
        self._snapshot: dict = {"t": 0, "buildings": [], "cores": psutil.cpu_count() or 1}
        sample()  # prime cpu_percent counters
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            time.sleep(self.interval)
            procs = sample()
            buildings = layout(procs, self.state)
            snap = {
                "t": time.time(),
                "cores": psutil.cpu_count() or 1,
                "buildings": [b.__dict__ for b in buildings],
            }
            with self._lock:
                self._snapshot = snap

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot


def make_handler(city: City):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if self.path.startswith("/api/snapshot"):
                body = json.dumps(city.snapshot()).encode()
                self._send(200, "application/json", body)
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:  # quiet
            pass

    return Handler


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="proccity", description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=2.0, help="sampling period, seconds")
    args = ap.parse_args(argv)
    city = City(args.interval)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(city))
    print(f"proccity: http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
