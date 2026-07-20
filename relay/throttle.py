# =============================================================================
# relay/throttle.py
# -----------------------------------------------------------------------------
# Responsible for: Rate-limiting failed login attempts along named DIMENSIONS
#                  (per-account, and a coarse relay-wide bound), with atomic
#                  increment/check/reset.
# Role in project: Used only by the relay's login path (relay/server.py).
#                  Passwords are guessable in a way 256-bit keys are not, so
#                  adding password login obliges adding throttling with it.
# Assumptions: Single relay process. State is in memory (see the module docstring
#              for why, and what that costs).
# =============================================================================
"""Dimension-keyed login throttling (auth revamp, Unit 3).

**Why dimensions rather than a single lockout counter.** Per-account lockout
alone stops someone guessing one person's password, but does nothing about an
attacker spreading attempts thinly across many names. A relay-wide bound catches
that. Both are the same mechanism differing only in what they key on, so they are
one implementation parameterised by a dimension name — which also means a future
`ip` dimension is a new key, not a redesign.

**Per-IP is deliberately NOT implemented here.** It needs the client IP, which
behind Fly's proxy means parsing a forwarded header — and trusting that header
without a correctly configured trust boundary lets an attacker forge it and evade
the limit entirely. That misconfiguration footgun is worse than the gap at two
accounts. The accepted trade, recorded honestly: an active attacker trips the
relay-wide bound and degrades login for everyone until they stop.

**State is in memory, not the database.** At this scale that is the simpler and
faster choice, and it keeps a hot path off SQLite. The cost, stated plainly: the
counters reset when the process restarts, so a deploy clears any active lockout,
and the Fly machine's idle auto-stop clears it too. That matters less than it
first appears — an attacker mid-attack is keeping the machine awake by definition,
so the lockout holds exactly while it is being exercised. A persistent backend
would slot in behind this same interface if that ever stops being true.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# --- Per-account lockout ------------------------------------------------------
# Five consecutive failures locks that account for fifteen minutes. Short enough
# that a legitimate person who mistyped is not meaningfully punished (and an admin
# can clear it instantly), long enough that online guessing is hopeless against
# any non-trivial password.
_ACCOUNT_MAX_FAILURES = 5
_ACCOUNT_LOCKOUT_SECONDS = 15 * 60

# --- Relay-wide bound ---------------------------------------------------------
# A ceiling on TOTAL failed attempts across all accounts in a rolling window. Set
# far above what humans generate (two accounts, occasional typos) and far below
# what a guessing run needs. This is the only thing standing between a distributed
# name-spraying attempt and unlimited tries.
_GLOBAL_MAX_FAILURES = 50
_GLOBAL_WINDOW_SECONDS = 5 * 60

ACCOUNT = "account"
GLOBAL = "global"


@dataclass
class _Counter:
    """Failure count plus the timestamp bounding its window or lockout."""

    failures: int = 0
    window_started: float = 0.0
    locked_until: float = 0.0


class LoginThrottle:
    """Tracks failed login attempts along named dimensions.

    Why:
        One lock guards every mutation, so increment / check / reset are atomic
        with respect to each other. Without that, two simultaneous failures could
        interleave read-modify-write and lose a count — which is precisely the
        race an attacker parallelising attempts would be exploiting.
    """

    def __init__(self, now=time.monotonic) -> None:
        """Create an empty throttle.

        Args:
            now: Clock function returning seconds. Injectable so tests can drive
                expiry deterministically instead of sleeping. Defaults to
                `time.monotonic`, which is immune to wall-clock adjustments — a
                system clock jump must never silently release a lockout.
        """
        self._now = now
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str], _Counter] = {}

    def is_blocked(self, dimension: str, key: str) -> bool:
        """Report whether this dimension+key is currently blocked.

        Args:
            dimension: ACCOUNT or GLOBAL.
            key: The account name, or "" for the global dimension.

        Returns:
            True when attempts should be refused.

        Why:
            The caller checks this BEFORE looking anything up, but must still run
            a full verification afterwards regardless — a blocked account has to
            cost the same as an unknown one, or the block itself becomes an oracle
            confirming the account exists.
        """
        with self._lock:
            counter = self._counters.get((dimension, key))
            if counter is None:
                return False
            if dimension == GLOBAL:
                self._expire_window_locked(counter)
                return counter.failures >= _GLOBAL_MAX_FAILURES
            return counter.locked_until > self._now()

    def record_failure(self, dimension: str, key: str) -> None:
        """Count one failed attempt, locking out or capping if it crosses a limit.

        Args:
            dimension: ACCOUNT or GLOBAL.
            key: The account name, or "" for the global dimension.

        Returns:
            None.

        Why:
            Recorded for the ACCOUNT dimension even when the name is unknown. If
            only real accounts were counted, the throttle's own behavior would
            reveal which names exist — the same enumeration leak the timing parity
            in `passwords.verify_password` exists to close.
        """
        with self._lock:
            counter = self._counters.setdefault((dimension, key), _Counter())
            now = self._now()
            if dimension == GLOBAL:
                self._expire_window_locked(counter)
                if counter.window_started == 0.0:
                    counter.window_started = now
                counter.failures += 1
                return
            if counter.locked_until > now:
                return  # already locked; extending it on each try would be a self-DoS lever
            counter.failures += 1
            if counter.failures >= _ACCOUNT_MAX_FAILURES:
                counter.locked_until = now + _ACCOUNT_LOCKOUT_SECONDS

    def record_success(self, key: str) -> None:
        """Clear an account's failure count after a genuine login.

        Args:
            key: The account name.

        Returns:
            None.

        Why:
            Only the ACCOUNT dimension resets. The global bound deliberately does
            NOT clear on success: an attacker with one valid credential could
            otherwise reset the relay-wide counter at will and spray indefinitely.
        """
        with self._lock:
            self._counters.pop((ACCOUNT, key), None)

    def reset(self, key: str) -> None:
        """Clear an account's lockout administratively.

        Args:
            key: The account name to unlock.

        Returns:
            None.

        Why:
            The escape hatch for the self-DoS case — someone hammering a real
            person's name locks that person out. An admin clears it immediately
            (this is what `relay-user password reset` calls) rather than the person
            waiting out the window.
        """
        with self._lock:
            self._counters.pop((ACCOUNT, key), None)

    def _expire_window_locked(self, counter: _Counter) -> None:
        """Reset a global counter whose rolling window has elapsed.

        Args:
            counter: The counter to age out. Caller MUST hold self._lock.

        Returns:
            None.

        Why:
            The global bound is a rolling window rather than a permanent ceiling,
            or the relay would lock itself out forever after fifty lifetime typos.
        """
        if (
            counter.window_started
            and self._now() - counter.window_started >= _GLOBAL_WINDOW_SECONDS
        ):
            counter.failures = 0
            counter.window_started = 0.0
