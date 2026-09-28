# proccity

Your running processes as a living 3D city. Each building is a process. The city changes as
your system changes: new processes rise out of the ground, dead ones sink, busy ones glow.

- **Height** = resident memory (log scale, so a 10x hog is visibly, not absurdly, taller)
- **Footprint** = thread count
- **Glow** = CPU (one full core lights the windows fully)
- **Colour** = owning user
- **District** = a top-level process and its descendants (a browser and its helpers, a shell
  and what it spawned). Loner processes are pooled into shared "commons" blocks so a typical
  400-process macOS/Linux table is a dense city, not a suburb.

Streets are stable: a process keeps its lot for life, and a released lot goes to the next
newcomer, so you can watch a district and see it breathe.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/proccity            # or: .venv/bin/python -m proccity
# open http://127.0.0.1:8765/
```

`--port` and `--interval` (sampling period, default 2s) are the only flags.

The server binds loopback only and has no auth. The snapshot exposes process names, users
and memory for this machine, which is fine on your own laptop and not something to put on a
network. The frontend loads Three.js from unpkg, so the page needs internet the first time.

## Test

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The layout is a pure module (`proccity/layout.py`) with no psutil or I/O, so the tests cover
district grouping, lot stability across snapshots, no-overlap, commons overflow, and the
log-scaled height, without touching a real process table.

## Built for fun

A weekend toy inspired by a reel. Not a monitoring tool; use `htop`, `btop`, or your APM for
that. MIT.
