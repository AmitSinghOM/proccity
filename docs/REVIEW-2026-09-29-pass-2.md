# Six-seat review, pass 2, 2026-09-29

Scope: main at `aa2268f`, restricted to surfaces pass 1 (`docs/REVIEW-2026-09-29.md`) did not
reach: the cartoon renderer and D-pad (both landed after pass 1), the sampler thread, packaging,
HTTP method handling, the wire format, and a new SDET seat. Seats: Staff, Product, Design,
Network, Security, SDET. Every accepted fix has a test written red-first; each was proven to
discriminate by stashing the source change and watching exactly that test fail on the old code
(9 of 9 layout/server tests failed on `aa2268f`; the wheel test and the browser test were each
broken deliberately once to confirm they detect the defect they exist for).

## Accepted

| # | Seat | Severity | Finding | Fix | Lock |
|---|---|---|---|---|---|
| R5 | Staff | Medium | `district_of_root` was never pruned for dead loners. Commons blocks are kept on purpose (pass 1, R1), so every short-lived process (each `ls`, each cron child) left an entry behind forever: 200 one-at-a-time loners left 200 entries. Also meant a recycled pid could inherit a stranger's district. | Delete `district_of_root` entries for dead pids before releasing districts. | `test_dead_loners_do_not_leak_root_bookkeeping`, `test_recycled_pid_of_a_dead_family_root_is_not_glued_to_the_old_district` |
| P3 | Product | Medium | A family larger than 64 members overflowed its block and the extra processes were **silently dropped** (`continue`). A browser with 70 helpers showed 64 buildings and a HUD population that disagreed with `ps`. On this host today the largest family is 39, so it had not fired yet. | Overflow spills into the commons (visible, loses its family colour). Never drop a process. | `test_a_family_bigger_than_a_block_spills_into_commons_instead_of_vanishing` |
| R6 | Staff / SDET | High | `package-data` matched `static/*.html` only. A clean wheel shipped `index.html` without `app.js`/`app.css`: `pip install proccity` served a blank page. Masked twice over: CI installs with `-e` (reads the tree), and an in-tree `build/lib/` from any earlier build is copied wholesale into the next wheel, so a developer build "worked". | Explicit `static/*.html`, `*.js`, `*.css`. | `test_wheel_ships_every_allowlisted_static_file` builds from a copy without `build/`/egg-info and inspects the zip |
| R3' | Staff | Low | Pass 1 added the sampler exception guard but left it untested ("would need fault injection"). Also no way to stop the loop; `time.sleep` in a `while True`. | `_safe_tick()` seam, `threading.Event`-driven loop, `City.stop()`, called from `main()`'s `finally`. | `test_sampler_survives_a_failing_sample_and_keeps_last_snapshot` injects a raising `sample()`, checks the last snapshot is kept, the thread lives, and `stop()` ends it |
| N2 | Network | Low | Geometry went over the wire unrounded (`17.400000000000002`, 15-digit widths) in an ~86 KB payload every two seconds. | Round x/z/width/height to 3 dp, cpu to 1 dp, `t` to ms. 86,163 → 76,820 bytes on 419 processes (−11%). | `test_geometry_is_rounded_for_the_wire`, `test_snapshot_floats_are_rounded_on_the_wire` |
| N3 | Network | Low | Unknown verbs got `http.server`'s default 501 HTML page naming Python; no `HEAD`. | `do_HEAD`; `POST/PUT/DELETE/PATCH/OPTIONS` → `405` + `Allow: GET, HEAD`; plain-text `error_message_format`. | `test_unsupported_methods_get_405_not_501_and_head_has_no_body`, `test_error_pages_are_plain_text_without_python_branding` |
| N4 | Network | Low | `protocol_version` was HTTP/1.0: one new TCP connection per poll. Every response already carries `Content-Length`, so keep-alive is safe. | `protocol_version = "HTTP/1.1"`. Verified with curl: "Re-using existing connection". | `test_connection_is_reused_across_polls` |
| N5 / P4 | Network / Product | Low | Client polled every 2000 ms regardless of `--interval` (faster: identical bytes; slower: missed samples). HUD "rent" summed RSS, which double-counts shared pages and can exceed physical RAM. | Snapshot carries `interval`, `mem_total`, `mem_used` (from `virtual_memory`); client re-arms its timer to the server cadence and shows "rent 83% of 16 GiB". | `test_snapshot_carries_interval_and_real_memory_numbers`; browser test reads the rendered HUD |
| S5 | Security | Low | `three/addons/` maps a whole CDN directory and `script-src` allowed all of `unpkg.com`, so a future `import 'three/addons/X.js'` (or any package on unpkg) would load with no integrity check. | `script-src` scoped to `https://unpkg.com/three@0.160.0/`; test resolves every import in `app.js` through the import map and requires an `integrity` entry. | `test_every_cdn_module_the_app_imports_has_an_integrity_hash_and_csp_is_version_scoped` |
| T1 | SDET | Medium | The only browser verification was a throwaway CDP script in `/tmp`. Nothing committed proved the CSP + SRI build actually runs; a wrong hash blanks the page while every unit test stays green. | `tests/test_browser.py`: headless Chrome via `--dump-dom` + `--enable-logging=stderr` (no DevTools client), fails on any console error/refusal, asserts the HUD reached "population N". Skips without Chrome; CI sets `PROCCITY_REQUIRE_BROWSER=1` so it cannot skip there. Harness self-check proves the console channel sees a `console.error`. | Broken deliberately (one hash character) → fails with "Failed to resolve module specifier three". `test_ci_requires_the_browser_smoke_test` |
| D2 | Design | Low | The `@media (max-width: 720px)` block duplicated the entire `#pad` rule set verbatim (copy-paste from the legend fix). Tooltip was `pointermove`-only, so touch users never saw one. | Media block reduced to the two `display: none` rules. `pointerdown` on the canvas aims the picking ray. | visual; browser test loads the CSS |

## Declined, with evidence

| Seat | Candidate | Why declined |
|---|---|---|
| Security | Vendor Three.js (again) | Same reasoning as pass 1; S5 now narrows the CDN allowance to one version path, which closes the "any module on unpkg" gap without the +700 KB. |
| Security | `Permissions-Policy` / `Cross-Origin-Opener-Policy` headers | Page uses no sensors, no popups, no cross-origin windows. Nothing to restrict. |
| Security | Auth token | Unchanged from pass 1: anything local can run `ps`. |
| Network | ETag / 304 on the snapshot | `t` changes every tick by construction; a conditional GET never hits. |
| Network | `Cache-Control: no-store` on static files is wasteful | ~30 KB re-fetched per page load on loopback. Not worth a cache-busting scheme. |
| Network | Compress the snapshot | Loopback; gzip CPU would cost more than 77 KB of loopback bytes. Rounding took the free 11%. |
| Staff | Give a big family several districts instead of spilling to commons | Correct picture, but `district_of_root` becomes a list and every stability rule needs re-proving. Spilling is honest (a different colour, visibly) and never loses a building. Revisit if a real host shows >64 routinely. |
| Staff | `psutil.process_iter` cache correctness for `cpu_percent` | Re-verified in pass 1; unchanged. |
| Product | Show the top panels on phones | The 3D canvas is the product on a phone; two panels would cover it. Tap-to-inspect (D2) gives the per-building answer instead. |
| Design | Colour-blind palette for districts | Colour is an arbitrary district id; the plate under each block and the tooltip carry the grouping. Hue conveys no ordered quantity. |
| SDET | Coverage floor with `pytest-cov` | Adds a dependency to a 600-line project whose test-to-code ratio is already >1; the browser test and wheel test cover the two classes of failure coverage numbers cannot see. |
| SDET | Pixel-diff screenshots | SwiftShader output differs across hosts; a DOM + console assertion is stable and catches the failure that matters (blank page). |

## Verification

- `ruff check .` clean; `pytest` 57 passed (15 layout, 40 server, 2 browser) on Python 3.12
  with Chrome 154 headless.
- Discrimination: with `proccity/` + `pyproject.toml` stashed, the nine new layout/server
  tests fail on `aa2268f`; wheel test fails on the old `package-data`; browser test fails on
  a corrupted CSP hash.
- Live (`proccity --port 8799`): `GET /api/snapshot` 200 76,820 B; `HEAD` 200 with headers
  and no body; `POST` 405; `Host: attacker.example` 421; curl reports "Re-using existing
  connection" across two requests; snapshot keys `cores, interval, mem_total, mem_used, t`.
- Not verified: the D-pad and tap-to-inspect on a real touch device (no device on this
  host); the browser test on GitHub's Ubuntu image (first CI run of this PR is the check).
