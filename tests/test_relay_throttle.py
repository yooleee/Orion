# =============================================================================
# tests/test_relay_throttle.py
# -----------------------------------------------------------------------------
# Responsible for: The login throttle's counting, lockout, expiry and reset logic.
# Role in project: Unit-level cover for relay/throttle.py. The server-level tests
#                  prove throttling works end to end through HTTP; these pin the
#                  edges that are awkward to reach through a request — window
#                  expiry, the global dimension, and the reset asymmetry.
# Assumptions: The clock is injected, so nothing here sleeps.
# =============================================================================
"""Unit tests for the dimension-keyed login throttle (auth revamp, Unit 3)."""

from relay.throttle import (
    ACCOUNT,
    GLOBAL,
    LoginThrottle,
    _ACCOUNT_MAX_FAILURES,
    _ACCOUNT_LOCKOUT_SECONDS,
    _GLOBAL_MAX_FAILURES,
    _GLOBAL_WINDOW_SECONDS,
)


class _Clock:
    """A hand-driven monotonic clock, so expiry is tested without sleeping.

    Why: lockout windows are minutes long. Sleeping through them would make the
    suite unusably slow, and sleeping *approximately* would make it flaky.
    """

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def test_an_account_locks_out_only_at_the_threshold():
    """Failures below the limit do not block; the Nth one does.

    Why this matters: locking too early punishes ordinary typos, and the boundary is
    exactly where an off-by-one would hide.
    """
    clock = _Clock()
    throttle = LoginThrottle(now=clock)

    for _ in range(_ACCOUNT_MAX_FAILURES - 1):
        throttle.record_failure(ACCOUNT, "dad")
        assert throttle.is_blocked(ACCOUNT, "dad") is False

    throttle.record_failure(ACCOUNT, "dad")
    assert throttle.is_blocked(ACCOUNT, "dad") is True


def test_a_lockout_expires_on_its_own():
    """The block lifts once the lockout window passes, with no admin action.

    Why this matters: a lockout that never expired would turn every mistyped password into
    a permanent support request.
    """
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    for _ in range(_ACCOUNT_MAX_FAILURES):
        throttle.record_failure(ACCOUNT, "dad")
    assert throttle.is_blocked(ACCOUNT, "dad") is True

    clock.advance(_ACCOUNT_LOCKOUT_SECONDS + 1)
    assert throttle.is_blocked(ACCOUNT, "dad") is False


def test_failing_while_locked_does_not_extend_the_lockout():
    """Continued attempts during a lockout do not push its expiry further out.

    Why this matters: if each attempt extended the window, an attacker could keep a real
    person locked out indefinitely by failing once a minute forever — turning the defence
    into a denial-of-service lever against the person it protects.
    """
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    for _ in range(_ACCOUNT_MAX_FAILURES):
        throttle.record_failure(ACCOUNT, "dad")

    clock.advance(_ACCOUNT_LOCKOUT_SECONDS - 10)
    throttle.record_failure(ACCOUNT, "dad")   # an attempt just before expiry
    clock.advance(11)
    assert throttle.is_blocked(ACCOUNT, "dad") is False  # still expired on the original schedule


def test_unknown_names_are_counted_too():
    """Failures against a name that does not exist are still tracked.

    Why this matters: if only real accounts were counted, the throttle's own behaviour would
    reveal which names exist — an enumeration oracle sitting right behind the generic 401 and
    the timing parity that were built to prevent exactly that.
    """
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    for _ in range(_ACCOUNT_MAX_FAILURES):
        throttle.record_failure(ACCOUNT, "ghost")
    assert throttle.is_blocked(ACCOUNT, "ghost") is True


def test_accounts_are_tracked_independently():
    """One account's lockout does not affect another's.

    Why this matters: the per-account dimension is meaningless if its keys leak into each
    other — one person mistyping would lock out everyone.
    """
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    for _ in range(_ACCOUNT_MAX_FAILURES):
        throttle.record_failure(ACCOUNT, "dad")
    assert throttle.is_blocked(ACCOUNT, "dad") is True
    assert throttle.is_blocked(ACCOUNT, "someone-else") is False


def test_success_clears_the_account_counter_but_not_the_global_one():
    """A good login resets that account's failures; the relay-wide count survives.

    Why this matters: the asymmetry is deliberate and load-bearing. If success cleared the
    global counter, an attacker holding one valid credential could reset the relay-wide bound
    at will between bursts and spray other names indefinitely.
    """
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    throttle.record_failure(ACCOUNT, "dad")
    throttle.record_failure(GLOBAL, "")
    throttle.record_success("dad")

    assert throttle.is_blocked(ACCOUNT, "dad") is False
    for _ in range(_GLOBAL_MAX_FAILURES - 1):
        throttle.record_failure(GLOBAL, "")
    # The pre-success global failure still counts toward the bound.
    assert throttle.is_blocked(GLOBAL, "") is True


def test_the_global_bound_catches_spraying_across_many_names():
    """Failures spread thinly across many names still trip the relay-wide limit.

    Why this matters: this is the case per-account lockout cannot see. An attacker trying five
    passwords against each of a hundred names never trips any single account's threshold, so
    without the global dimension the throttle would be blind to exactly the attack a
    multi-account relay invites.
    """
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    for i in range(_GLOBAL_MAX_FAILURES):
        throttle.record_failure(ACCOUNT, f"name-{i}")   # never repeats a name
        throttle.record_failure(GLOBAL, "")

    assert throttle.is_blocked(GLOBAL, "") is True
    assert throttle.is_blocked(ACCOUNT, "name-0") is False  # no individual account tripped


def test_the_global_window_rolls_over():
    """The relay-wide count is a rolling window, not a permanent ceiling.

    Why this matters: a lifetime total would eventually lock the whole relay out over months
    of ordinary typos, which is an outage caused entirely by the defence.
    """
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    for _ in range(_GLOBAL_MAX_FAILURES):
        throttle.record_failure(GLOBAL, "")
    assert throttle.is_blocked(GLOBAL, "") is True

    clock.advance(_GLOBAL_WINDOW_SECONDS + 1)
    assert throttle.is_blocked(GLOBAL, "") is False


def test_admin_reset_clears_a_lockout_immediately():
    """reset() unblocks an account without waiting out the window.

    Why this matters: the counterweight that lets the lockout stay strict. Anyone who knows an
    account name can lock its owner out, so an instant admin unlock is what keeps that from
    being a real denial of service.
    """
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    for _ in range(_ACCOUNT_MAX_FAILURES):
        throttle.record_failure(ACCOUNT, "dad")
    assert throttle.is_blocked(ACCOUNT, "dad") is True

    throttle.reset("dad")
    assert throttle.is_blocked(ACCOUNT, "dad") is False
