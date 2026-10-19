"""Is this process what it says it is? Three honest states, never "safe".

- **verified**   -- the running code carries a valid signature from a real signer: Apple's
                    own chain, or a Developer ID / App Store Team ID. We can name the signer.
- **unverified** -- it runs, but the signature is missing, ad-hoc (self-signed with no
                    authority), or invalid (code modified after signing).
- **unknown**    -- we could not check: not yet reached this tick, process gone, or a platform
                    with no check implemented.

macOS first. Apple's `codesign` can verify a *running* process by pid, so the check is on the
code actually in memory, not a file on disk that might have been swapped since. Two facts
learned measuring it: `codesign --verify` exits 0 for an ad-hoc signature, so validity alone
is not enough -- a signer is required for "verified"; and it costs ~14 ms, so results are
cached per (pid, create_time) and at most `budget` new processes are checked per tick.

Windows (WinVerifyTrust) and Linux (package ownership) report "unknown" for now: a badge that
can be fooled is worse than no badge, and each needs its own careful implementation.

This is the third subprocess in proccity, on the same leash as the other two: absolute
path, literal argv, the only variable argument is a pid validated as a positive integer,
timeout, no shell, parsed by pure functions. The display output names the executable path;
the parser keeps only the fields below and discards the rest.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger("proccity.identity")

CODESIGN = "/usr/bin/codesign"
TIMEOUT = 3.0
BUDGET = 8                       # new processes checked per tick (2 calls each, ~37 ms)
APPLE_ROOT = "Apple Root CA"

VERIFIED, UNVERIFIED, UNKNOWN = "verified", "unverified", "unknown"


@dataclass(frozen=True)
class Identity:
    state: str              # verified | unverified | unknown
    signer: str = ""        # "Apple", a 10-char Team ID, "ad-hoc", "unsigned", "invalid", ""


UNKNOWN_ID = Identity(UNKNOWN)


# ---- pure parsers -------------------------------------------------------------------------

def parse_display(text: str) -> dict[str, str | list[str]]:
    """`codesign -dv <pid>` (stderr) -> the few fields we keep.

    Lines look like `Identifier=com.apple.finder`, `TeamIdentifier=43AQ936H96` or
    `TeamIdentifier=not set`, `Signature=adhoc`, one or more `Authority=...`, and for
    binaries that ship with the OS a `Platform identifier=26` line (those carry no Authority
    lines when displayed by pid). The `Executable=/path` line is deliberately not kept.
    """
    out: dict[str, str | list[str]] = {"authorities": []}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "Authority":
            out["authorities"].append(value)             # type: ignore[union-attr]
        elif key in ("Identifier", "TeamIdentifier", "Signature", "Platform identifier"):
            out[key] = value
    return out


def _classify_failure(text: str) -> Identity:
    """verify exited non-zero: say why, in the reader's words."""
    if "No such process" in text:
        return UNKNOWN_ID
    if "not signed at all" in text:
        return Identity(UNVERIFIED, "unsigned")
    if "invalid signature" in text or "modified" in text:
        return Identity(UNVERIFIED, "invalid")
    return Identity(UNVERIFIED, "")


def _classify_signed(display: dict) -> Identity:
    """verify passed: a signature exists and matches, but who vouches for it?"""
    team = str(display.get("TeamIdentifier", "")).strip()
    authorities = display.get("authorities") or []
    if team and team != "not set":
        return Identity(VERIFIED, team)
    if APPLE_ROOT in authorities or display.get("Platform identifier"):
        return Identity(VERIFIED, "Apple")
    if str(display.get("Signature", "")) == "adhoc" or not authorities:
        return Identity(UNVERIFIED, "ad-hoc")
    return Identity(UNVERIFIED, "")


def classify(verify_rc: int | None, verify_text: str, display: dict) -> Identity:
    """Turn a verify exit code plus the display fields into one of the three states."""
    if verify_rc is None:
        return UNKNOWN_ID
    if verify_rc != 0 or "No such process" in verify_text:
        return _classify_failure(verify_text)
    return _classify_signed(display)


def _run(argv: tuple[str, ...]) -> tuple[int | None, str]:
    try:
        # Literal argv, absolute path, no shell; the pid was validated by the caller.
        p = subprocess.run(argv, capture_output=True, text=True,  # noqa: S603
                           timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return p.returncode, p.stdout + p.stderr


# ---- the checker ---------------------------------------------------------------------------

class IdentityChecker:
    def __init__(self, enabled: bool | None = None, budget: int = BUDGET) -> None:
        self.enabled = sys.platform == "darwin" if enabled is None else enabled
        self.budget = budget
        self.source = "codesign" if self.enabled else "unknown"
        self._cache: dict[tuple[int, float], Identity] = {}
        self._failed = False

    def check(self, pid: int) -> Identity:
        """One process, two codesign calls. Public for tests; the sampler uses refresh()."""
        if not self.enabled or not isinstance(pid, int) or pid <= 0:
            return UNKNOWN_ID
        rc, text = _run((CODESIGN, "--verify", str(pid)))
        if rc is None:
            if not self._failed:
                log.warning("codesign unavailable; identity will read unknown")
                self._failed = True
            return UNKNOWN_ID
        self._failed = False
        display: dict = {}
        if rc == 0:
            _, dtext = _run((CODESIGN, "-dv", str(pid)))
            display = parse_display(dtext)
        return classify(rc, text, display)

    def refresh(self, procs: list, priority) -> dict[int, Identity]:
        """Return identities for every proc; check up to `budget` uncached ones this tick,
        most important first (the caller's `priority` key), and forget dead processes.

        `procs` need pid and create_time attributes. A reused pid has a new create_time, so a
        cached verdict can never be inherited by a stranger.
        """
        live = {(p.pid, p.create_time) for p in procs}
        for key in [k for k in self._cache if k not in live]:
            del self._cache[key]
        if self.enabled:
            todo = sorted((p for p in procs if (p.pid, p.create_time) not in self._cache),
                          key=priority)
            for p in todo[: self.budget]:
                self._cache[(p.pid, p.create_time)] = self.check(p.pid)
        return {p.pid: self._cache.get((p.pid, p.create_time), UNKNOWN_ID) for p in procs}
