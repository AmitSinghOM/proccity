"""SDET: the page must actually render under its own CSP + SRI in a real browser.

The unit tests prove the CSP hash matches the import map bytes; only a browser proves the
policy lets the app run (one wrong hash blanks the page silently). This drives headless Chrome
with `--dump-dom` and reads the console from `--enable-logging=stderr`, so no DevTools client
is needed. Needs a Chrome binary and network access to unpkg; skips without Chrome unless
PROCCITY_REQUIRE_BROWSER=1 (CI sets it, so a missing browser fails loudly there).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest

from proccity.server import City, make_handler

CANDIDATES = [
    os.environ.get("PROCCITY_CHROME", ""),
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def chrome_binary() -> str | None:
    for c in CANDIDATES:
        if c and (shutil.which(c) or os.path.isfile(c)):
            return shutil.which(c) or c
    return None


CHROME = chrome_binary()
if CHROME is None and os.environ.get("PROCCITY_REQUIRE_BROWSER") == "1":
    pytest.fail("PROCCITY_REQUIRE_BROWSER=1 but no Chrome binary found", pytrace=False)
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome binary on this host")


def run_chrome(url: str, profile: str, budget_ms: int = 8000) -> tuple[str, list[str]]:
    """Load `url`, let virtual time run so the first poll lands, return (DOM, console lines).

    Chrome does not exit on its own here: the page's requestAnimationFrame loop keeps virtual
    time from ever settling. So read stdout until the DOM dump is complete, then kill it."""
    cmd = [CHROME, "--headless=new", "--no-first-run", "--no-default-browser-check",
           "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1200,800",
           f"--user-data-dir={profile}", "--enable-logging=stderr", "--v=0",
           f"--virtual-time-budget={budget_ms}", "--dump-dom", url]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
    err: list[bytes] = []
    t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    t.start()
    out = bytearray()
    deadline = threading.Event()
    threading.Timer(90, deadline.set).start()
    while not deadline.is_set():
        chunk = p.stdout.read1(65536)
        if not chunk:
            break
        out += chunk
        if b"</html>" in out:
            break
    p.kill()
    p.wait(timeout=10)
    t.join(timeout=10)
    stderr = (err[0] if err else b"").decode("utf-8", "replace")
    # Chrome logs page console output as `...INFO:CONSOLE:12] "msg"` (older builds: `CONSOLE(12)`)
    console = [ln for ln in stderr.splitlines() if ":CONSOLE" in ln or "CONSOLE(" in ln]
    return out.decode("utf-8", "replace"), console


@pytest.fixture(scope="module")
def server():
    city = City(interval=60, start=False)
    city.tick()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(city))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_console_channel_actually_reports_errors(tmp_path):
    """Harness self-check: an empty console on the real page means nothing unless the harness
    can be shown to see an error when there is one."""
    _, console = run_chrome("data:text/html,<script>console.error('proccity-harness-boom')"
                            "</script><p>ok</p>", str(tmp_path / "p"), budget_ms=500)
    assert any("proccity-harness-boom" in ln for ln in console), console


def test_page_renders_under_csp_with_zero_console_errors(server, tmp_path):
    dom, console = run_chrome(f"http://127.0.0.1:{server}/", str(tmp_path / "p"))
    bad = [ln for ln in console
           if re.search(r"Refused to|Uncaught|Failed to load|integrity|ERROR:CONSOLE", ln)
           and "frame-ancestors" not in ln]           # meta CSP cannot carry frame-ancestors
    assert not bad, "\n".join(bad)
    # the first poll landed: the HUD reads "population N" with N > 0 and the panels have rows
    m = re.search(r"population (\d+)", re.sub(r"</?b>", "", dom))
    assert m and int(m.group(1)) > 0, dom[:2000]
    assert re.search(r'<ol id="topcpu">\s*<li>', dom) and re.search(r'<ol id="topmem">\s*<li>', dom)
    assert "city hall is not answering" not in dom
