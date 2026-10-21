"""Turn cumulative per-pid counters into per-second rates.

Both the network and the disk counters the OS hands out are "bytes since the process
started", so a rate needs two samples: keep the previous counters, divide the difference by
the elapsed time. Shared here so netio and diskio agree on the edge cases:

- the first sample for a pid yields no rate (the caller decides whether that is 0 or "not
  yet"), the same way psutil's first cpu_percent is 0;
- a counter that went *down* means the pid was reused by a new process; no rate either;
- a pid absent from the new sample is forgotten.
"""
from __future__ import annotations


class RateTracker:
    def __init__(self) -> None:
        self._last: dict[int, int] = {}
        self._last_t: float | None = None

    @property
    def last(self) -> dict[int, int]:
        """The previous counters: exactly one sample of state, so tests can check it stays
        bounded by the live pid set."""
        return self._last

    def rates_from(self, counters: dict[int, int], now: float) -> dict[int, float]:
        """Pure step: fresh cumulative counters in, rates against the previous call out."""
        rates: dict[int, float] = {}
        if self._last_t is not None:
            dt = now - self._last_t
            if dt > 0:
                for pid, total in counters.items():
                    prev = self._last.get(pid)
                    if prev is not None and total >= prev:
                        rates[pid] = (total - prev) / dt
        self._last, self._last_t = counters, now
        return rates
