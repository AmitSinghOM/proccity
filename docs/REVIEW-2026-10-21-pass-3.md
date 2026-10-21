# Three-seat review, pass 3, 2026-10-21

Seats: Staff engineer, Product, Security. Scope: only surfaces passes 1 and 2 never reached --
the four subprocess adapters (`netio`, `focus`, `identity`), the two new `ctypes`/libproc
adapters (`diskio`, `threads`), the report and its leakage contract, the HUD disk panel, and
the README's factual claims. Layout stability, packaging, HTTP headers and CSP were covered
in passes 1-2 and were not re-run.

Method: each file read in full at `838434e`; each candidate argued for and against before
acceptance; each accepted fix locked by a test shown to **fail on the pre-fix code** (stash,
run, restore); every measurement below was taken on this host today. Run inline, not by a
subagent (two prior review subagents hung for their full idle deadline with no output).

## Accepted

| # | Seat | Severity | Confidence | Finding | Fix | Lock |
| --- | --- | --- | --- | --- | --- | --- |
| R7 | Staff / Security | Medium | 85% | `focus._x11_screen` runs one `xprop` subprocess per mapped window. Pass 2's `MAX_WINDOWS = 64` caps the **count**, not the **clock**: with a hung X server every call hits `TIMEOUT` (2 s), so one tick can take 64 × 2 s = 128 s and the sampler thread -- the only one -- freezes the city for two minutes. The `_loop` guards against overlap, not against a long tick. | Wall-clock budget `X11_BUDGET = 1.0` s measured after the root call; the loop stops at the deadline, keeps the partial set, logs once (`_slow`). A hung server now costs one tick ≈ one timeout. | `test_x11_sampler_stops_querying_windows_once_the_time_budget_is_spent` -- fakes `time.monotonic` to advance 0.6 s per call; 2 of 3 windows queried. Fails on `838434e` (3 calls). |
| P7 | Product | Low | 90% | The "disk" column on Windows reads `GetProcessIoCounters`, whose `ReadTransferCount`/`WriteTransferCount` count **all** I/O transfers (files, pipes, devices), not storage alone. The column header and README said "storage" for every platform. | Caveat in `diskio.py` docstring, in the report header line for the `psutil` source, and in the README matrix cell. | README claim audit (every row of the matrix re-read against the source it names). |
| S6 | Security | Low | 80% | libproc thread **handles** returned by `PROC_PIDLISTTHREADS` are kernel thread addresses (KASLR-sensitive). They are used only as dict keys inside `threads.py` and never reach a `Row`, but nothing *asserted* that. | `FORBIDDEN_KEYS` in `test_leakage.py` gains `tid`, `tids`, `thread_id(s)`, `handles`, `thread_names`; the allowlist walk already checks every key at every depth of the live JSON report. | `test_report_rows_carry_exactly_the_allowlisted_fields_and_no_forbidden_key_anywhere` (extended). |
| P8 | Product | Low | 85% | HUD had no view of disk activity; the report had the column but the city did not. A padded top-5 of `0 B/s` rows would have said nothing. | "💾 Hitting the disk" list: only processes ≥ 4 KiB/s, else an honest "disk is quiet right now" / "not measured on this platform" line driven by `sources.disk`. | Visual; browser test loads the page. (Headless Chrome crashes in the build sandbox; CI runs it.) |

## Declined, with evidence

| Seat | Candidate | Why declined |
| --- | --- | --- |
| Staff | The new samplers make the tick too expensive for a 2 s interval | Measured on 552 processes: psutil sample 0.129 s, **disk 0.002 s, threads 0.008 s**, nettop 0.038 s, counts 0.125 s, identity 0.464 s. The two new signals are 1% of the tick. The cost that matters is identity (8 `codesign` subprocesses), already budgeted and filling progressively by design. |
| Staff | `read_libproc` allocates a 32 KiB handle buffer per pid per tick (~17 MB churn / tick) | Same measurement: 8 ms for all 552 pids. A shared buffer would save allocation the GC already reclaims for free; not worth a mutable field on the sampler. |
| Security | `proc_pidinfo` return value could exceed the buffer and over-read `handles` | The kernel copies at most `buffersize` bytes and returns the bytes copied; `got // 8 ≤ MAX_THREADS`. Verified against the header contract and by the live test reading this process (`test_live_libproc_reads_own_process_and_refuses_pid_zero`). |
| Security | `ctypes.CDLL(find_library("c"))` with `find_library` returning `None` | `CDLL(None)` resolves the main program's symbol table, where libc is already linked; the `except (OSError, AttributeError)` turns any miss into `source = "unavailable"`. Degrades, never crashes. |
| Security | Thread split for other users' processes on Linux is a cross-user information disclosure | `/proc/<pid>/task/*/stat` is world-readable by kernel design; `ps -L` shows the same numbers. proccity reports two integers (count, share), fewer than `ps`. README says "your own processes" for the split, which is conservative on Linux and exact on macOS/Windows. |
| Product | Second threshold (4 KiB/s in the HUD vs 1 MiB/s busy in the report) is inconsistent | They answer different questions: the HUD lists the top writers (like the CPU and RAM panels, which have no threshold); the report's 1 MiB/s decides the word *busy*. The 4 KiB/s floor only removes `0 B/s` padding. |
| Product | Add disk and thread hints to the legend footer | The legend describes building shape and colour; disk and threads are not drawn on buildings. The HUD heading and tooltip carry them. |
| Security | A process could spoof its disk/thread numbers to look idle | The counters are kernel accounting the process cannot write to. It can of course *be* idle; proccity reports what it does, not what it is. |
| Staff | `enrich` walrus-truthiness on dataclass instances (`if ident := ...`) | `Identity`, `Counts`, `ThreadSplit` define neither `__bool__` nor `__len__`, so an instance is always truthy. Pinned by the existing enrich tests which pass through zero-valued counts. |

## Verification

- `ruff check .` clean; 159 tests pass (2 browser tests deselected -- they fail identically on
  `main` in this sandbox and run in CI); cqa gate `--new-findings-only --fail-on warning`: 0.
- R7 lock test shown to fail on `838434e` and pass after the fix.
- Live on this host: disk and thread columns populate for every own-user row; `python3.12` at
  1.0 MiB/s shows in the new HUD list; `KiroCrew Helper (Renderer)` reads `35 (8 hot, 81%)`.
