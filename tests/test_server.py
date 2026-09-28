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
