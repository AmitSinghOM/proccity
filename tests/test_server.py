import base64
import hashlib
import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer

import pytest

from proccity.server import STATIC, City, host_is_loopback, main, make_handler


@pytest.fixture(scope="module")
def server():
    city = City(interval=60, start=False)
    city.tick()                       # one real sample, no background thread in tests
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(city))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host} if host is not None else {}
    c.request("GET", path, headers=headers)
    r = c.getresponse()
    body = r.read()
    return r.status, dict(r.getheaders()), body


# ---- Security: DNS rebinding -----------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "127.0.0.1", "LOCALHOST",
                                  "[::1]:8765"])
def test_loopback_hosts_are_accepted(host):
    assert host_is_loopback(host)


@pytest.mark.parametrize("host", [None, "", "attacker.example", "attacker.example:8765",
                                  "127.0.0.1.attacker.example", "localhost.attacker.example",
                                  "0.0.0.0", "10.0.0.5:8765", "[::ffff:127.0.0.1]:8765"])
def test_foreign_hosts_are_refused(host):
    assert not host_is_loopback(host)


def test_rebound_host_gets_421_and_no_data(server):
    status, _, body = get(server, "/api/snapshot", host="attacker.example")
    assert status == 421
    assert b"buildings" not in body


def test_loopback_host_gets_snapshot(server):
    status, headers, body = get(server, "/api/snapshot", host=f"127.0.0.1:{server}")
    assert status == 200
    snap = json.loads(body)
    assert snap["buildings"] and {"pid", "x", "z", "height"} <= set(snap["buildings"][0])
    assert headers["Content-Type"] == "application/json"


# ---- Security: static allowlist and headers --------------------------------------------

@pytest.mark.parametrize("path", ["/static/../server.py", "/static/app.js/../../pyproject.toml",
                                  "/proccity/server.py", "/static/", "/static/index.html"])
def test_only_allowlisted_files_are_served(server, path):
    status, _, _ = get(server, path, host="127.0.0.1")
    assert status == 404


def test_security_headers_on_every_response(server):
    for path in ("/", "/static/app.js", "/api/snapshot", "/nope"):
        _, h, _ = get(server, path, host="127.0.0.1")
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["Cache-Control"] == "no-store"
        assert h["Referrer-Policy"] == "no-referrer"
        assert h["X-Frame-Options"] == "DENY"
        assert "Python" not in h.get("Server", "")


def test_query_string_does_not_defeat_routing(server):
    status, _, _ = get(server, "/api/snapshot?x=1", host="127.0.0.1")
    assert status == 200


# ---- Security: CSP hash locks the inline import map ------------------------------------

def test_csp_hash_matches_the_import_map_bytes():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
    assert m, "import map missing"
    digest = base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode()
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert f"'sha256-{digest}'" in csp, "CSP hash drifted from the import map content"
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp


def test_import_map_pins_versions_and_integrity():
    imap = json.loads(re.search(r'<script type="importmap">(.*?)</script>',
                                (STATIC / "index.html").read_text(), re.S).group(1))
    for url in imap["imports"].values():
        assert re.search(r"@\d+\.\d+\.\d+/", url), f"unpinned CDN url {url}"
    assert set(imap["integrity"]) >= {imap["imports"]["three"]}
    assert all(v.startswith("sha384-") for v in imap["integrity"].values())


def test_every_cdn_module_the_app_imports_has_an_integrity_hash_and_csp_is_version_scoped():
    """Review 2, Security S5: `three/addons/` maps a whole directory and CSP allowed all of
    unpkg.com, so a future `import 'three/addons/X.js'` would load with no integrity check
    from any package on the CDN. Resolve each import the app makes and require a hash; scope
    script-src to the pinned version's path."""
    html = (STATIC / "index.html").read_text()
    imap = json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1))
    js = (STATIC / "app.js").read_text()
    specs = re.findall(r"""from\s+['"]([^'"]+)['"]""", js)
    assert specs, "no imports found"
    resolved = []
    for spec in specs:
        if spec in imap["imports"]:
            resolved.append(imap["imports"][spec])
            continue
        prefix = next(p for p in imap["imports"] if p.endswith("/") and spec.startswith(p))
        resolved.append(imap["imports"][prefix] + spec[len(prefix):])
    for url in resolved:
        assert url in imap["integrity"], f"{url} has no integrity hash"
    version = re.search(r"three@(\d+\.\d+\.\d+)/", imap["imports"]["three"]).group(1)
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)
    script_src = re.search(r"script-src ([^;]+)", csp).group(1).split()
    cdn = [s for s in script_src if s.startswith("https://")]
    assert cdn == [f"https://unpkg.com/three@{version}/"], cdn


def test_frontend_has_no_inline_handlers_or_innerhtml():
    js = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    assert "innerHTML" not in js
    assert not re.search(r"\son\w+=", html)


# ---- Staff: CLI validation --------------------------------------------------------------

@pytest.mark.parametrize("argv", [["--interval", "0"], ["--interval", "-1"], ["--port", "0"],
                                  ["--port", "70000"]])
def test_bad_arguments_are_rejected(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2


# ---- Design: the D-pad is real, labelled, and every key has a button twin ----------------

def test_dpad_has_nine_labelled_controls():
    html = (STATIC / "index.html").read_text()
    pad = re.search(r'<nav id="pad".*?</nav>', html, re.S).group(0)
    buttons = re.findall(r'<button type="button" data-k="(\w+)" aria-label="([^"]+)"', pad)
    assert [k for k, _ in buttons] == ["rotl", "up", "rotr", "left", "down", "right",
                                       "in", "home", "out"]
    assert all(label for _, label in buttons)
    js = (STATIC / "app.js").read_text()
    for k in ("up", "down", "left", "right", "rotl", "rotr", "in", "out", "home"):
        assert f"'{k}'" in js, f"key action {k} has no handler"



# ---- Review 2: packaging, sampler resilience, methods, payload ---------------------------

def test_wheel_ships_every_allowlisted_static_file(tmp_path):
    """Staff R6 (SDET): package-data only matched `static/*.html`, so a clean wheel shipped
    index.html without app.js/app.css and served a blank page to anyone who `pip install`ed
    it. Two things masked it: CI installs with `-e` (reads the source tree), and a stale
    in-tree `build/lib/` from any earlier build is copied into the next wheel wholesale.
    So build from a copy WITHOUT build/ or egg-info and look inside the wheel."""
    import shutil
    import subprocess
    import sys
    import zipfile

    from proccity.server import STATIC_FILES

    root = STATIC.parent.parent
    src = tmp_path / "src"
    shutil.copytree(root, src, ignore=shutil.ignore_patterns(
        ".git", ".venv", "build", "dist", "*.egg-info", "__pycache__", ".ruff_cache", "docs"))
    out = tmp_path / "whl"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(out),
                    str(src)], check=True, capture_output=True)
    names = set(zipfile.ZipFile(next(out.glob("proccity-*.whl"))).namelist())
    for fname, _ in STATIC_FILES.values():
        assert f"proccity/static/{fname}" in names, f"{fname} missing from wheel"


def test_sampler_survives_a_failing_sample_and_keeps_last_snapshot(monkeypatch):
    """Staff R3 (left untested in review 1): one psutil hiccup must not kill the only sampler
    thread or blank the city. Inject the fault, run one loop iteration, check the snapshot."""
    import proccity.server as srv

    city = City(interval=0.01, start=False)
    city.tick()
    before = city.snapshot_bytes()

    def broken():
        raise RuntimeError("psutil sneezed")

    monkeypatch.setattr(srv, "sample", broken)
    city._safe_tick()                                   # what the loop calls; must not raise
    assert city.snapshot_bytes() == before              # last good snapshot kept, not blanked

    # and the real loop keeps going after a failure: run it for a few ticks then stop it
    t = threading.Thread(target=city._loop, daemon=True)
    t.start()
    threading.Event().wait(0.05)
    assert t.is_alive(), "loop died after a failing sample"
    city.stop()
    t.join(timeout=2)
    assert not t.is_alive(), "stop() did not end the loop"


def test_unsupported_methods_get_405_not_501_and_head_has_no_body(server):
    """Network N3: http.server answers an unknown verb with 501 and a Python-flavoured HTML
    page. A loopback tool should say 405 with Allow: GET, and HEAD must mirror GET headers."""
    c = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    c.request("POST", "/api/snapshot", body=b"{}", headers={"Host": "127.0.0.1"})
    r = c.getresponse()
    r.read()
    assert r.status == 405 and r.getheader("Allow") == "GET, HEAD"
    c = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    c.request("HEAD", "/api/snapshot", headers={"Host": "127.0.0.1"})
    r = c.getresponse()
    body = r.read()
    assert r.status == 200 and body == b"" and r.getheader("Content-Type") == "application/json"
    assert int(r.getheader("Content-Length")) > 2


def test_snapshot_floats_are_rounded_on_the_wire(server):
    """Network N2: no 17.400000000000002 in an 80 KB payload sent every two seconds."""
    _, _, body = get(server, "/api/snapshot", host="127.0.0.1")
    assert not re.search(rb"\d\.\d{4,}", body), "unrounded float on the wire"


def test_connection_is_reused_across_polls(server):
    """Network N4: HTTP/1.0 closed the socket after every response, so the browser opened a new
    TCP connection every two seconds. With Content-Length on every reply, keep-alive is safe."""
    c = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    for _ in range(3):                                   # would raise on a closed socket
        c.request("GET", "/api/snapshot", headers={"Host": "127.0.0.1"})
        r = c.getresponse()
        r.read()
        assert r.status == 200 and r.version == 11
        assert r.getheader("Connection", "").lower() != "close"
    c.close()


def test_snapshot_carries_interval_and_real_memory_numbers(server):
    """Product P4 / Network N5: the HUD summed RSS ("rent 7.3 GiB"), which double-counts shared
    pages and can exceed physical RAM; and the client polled every 2 s regardless of
    --interval. The snapshot now says what the machine really has and how often to ask."""
    _, _, body = get(server, "/api/snapshot", host="127.0.0.1")
    snap = json.loads(body)
    assert snap["interval"] == 60
    assert snap["mem_total"] > snap["mem_used"] > 0
    assert sum(b["rss"] for b in snap["buildings"]) > 0


def test_error_pages_are_plain_text_without_python_branding(server):
    c = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    c.putrequest("BREW", "/api/snapshot", skip_host=True)
    c.putheader("Host", "127.0.0.1")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    assert r.status == 501
    assert r.getheader("Content-Type", "").startswith("text/plain")
    assert b"<html" not in body.lower() and b"python" not in body.lower()


def test_ci_requires_the_browser_smoke_test():
    """SDET: a skip in CI is a silent hole. The workflow must set the REQUIRE flag."""
    ci = (STATIC.parent.parent / ".github" / "workflows" / "ci.yml").read_text()
    assert 'PROCCITY_REQUIRE_BROWSER: "1"' in ci
