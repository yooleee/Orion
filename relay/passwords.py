# =============================================================================
# relay/passwords.py
# -----------------------------------------------------------------------------
# Responsible for: Hashing and verifying interactive-account passwords with
#                  Argon2id, under two constraints that are easy to get subtly
#                  wrong — TIMING PARITY across every failure class, and a hard
#                  BOUND on concurrent memory use.
# Role in project: Used only by the relay's login path (relay/server.py). The
#                  producer never imports this, so `argon2-cffi` stays a
#                  relay-deployment dependency and the core install keeps its
#                  three runtime deps.
# Assumptions: argon2-cffi is installed wherever password login is enabled. The
#              relay fails CLOSED (refuses password auth with a clear error)
#              rather than degrading to a weaker scheme when it is missing.
# =============================================================================
"""Argon2id password hashing for interactive accounts (auth revamp, Unit 3).

Why a separate module rather than a few functions in server.py: the two
properties below are the whole security value of this unit, and both are the
kind of thing that quietly rots when interleaved with request plumbing.

1. **Timing parity.** A login must take the same observable time whether the
   name is unknown, the account has no password, the account is locked out, or
   the password is simply wrong. Otherwise the response time itself answers "does
   this account exist?" — which is exactly the enumeration the generic 401 was
   introduced to prevent. We get parity by running EXACTLY ONE Argon2
   verification on every attempt, against a server-held dummy hash when there is
   no real one to check.

2. **Bounded memory.** Argon2id is deliberately memory-hard, and the relay runs
   on ThreadingHTTPServer, which spawns an unbounded thread per request. Those
   two facts multiply: without a bound, N simultaneous login attempts allocate
   N × memory_cost, and a few dozen requests can OOM-kill a small VM. That is an
   unauthenticated denial of service, so concurrency is capped here rather than
   left to chance.
"""

from __future__ import annotations

import threading

# --- Argon2id parameters ------------------------------------------------------
# OWASP's first recommended Argon2id configuration (m=19 MiB, t=2, p=1). Argon2id
# is the current first-choice password hash: memory-hard (so GPU/ASIC cracking
# gains little over CPU) and side-channel-resistant. The encoded hash embeds its
# own salt AND these parameters, so a stored hash stays verifiable after a
# parameter change — which is what makes `check_needs_rehash` upgrades possible.
_MEMORY_COST_KIB = 19456   # 19 MiB per verification
_TIME_COST = 2
_PARALLELISM = 1
_HASH_LEN = 32
_SALT_LEN = 16

# --- Concurrency bound --------------------------------------------------------
# Peak hashing memory is (cap × _MEMORY_COST_KIB) ≈ 76 MiB at cap=4, which fits
# comfortably in the 512 MB Fly VM alongside the Python process. Requests beyond
# the cap WAIT rather than fail: backpressure degrades latency under attack
# instead of killing the machine, and legitimate logins still complete. Raising
# the VM would raise the threshold; only a cap removes the vector.
_MAX_CONCURRENT_VERIFICATIONS = 4
_verification_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_VERIFICATIONS)

# A password longer than this is rejected BEFORE hashing. Argon2's cost is
# dominated by memory rather than input length, but an unbounded input is still
# free work handed to an attacker, and no real passphrase needs more.
MAX_PASSWORD_BYTES = 1024


class PasswordsUnavailable(RuntimeError):
    """Raised when password auth is requested but argon2-cffi is not installed.

    Why:
        Fail-closed. The alternative — silently falling back to a weaker hash, or
        treating every password as wrong — either weakens security invisibly or
        produces an outage no one can diagnose. A named error at the boundary
        says exactly what is missing.
    """


def _hasher():
    """Build a configured argon2 PasswordHasher, importing the dependency lazily.

    Returns:
        An argon2.PasswordHasher with this module's explicit parameters.

    Raises:
        PasswordsUnavailable: when argon2-cffi is not installed.

    Why:
        The import is lazy and local so that merely importing this module (which
        the relay does unconditionally) never requires the dependency — only
        actually using password auth does. Parameters are passed EXPLICITLY
        rather than relying on library defaults, so an argon2-cffi upgrade cannot
        silently change the cost of every stored hash.
    """
    try:
        from argon2 import PasswordHasher
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise PasswordsUnavailable(
            "password login needs the 'argon2-cffi' package, which is not installed. "
            "Install the relay extra (pip install '.[relay]') and redeploy."
        ) from exc
    return PasswordHasher(
        time_cost=_TIME_COST,
        memory_cost=_MEMORY_COST_KIB,
        parallelism=_PARALLELISM,
        hash_len=_HASH_LEN,
        salt_len=_SALT_LEN,
    )


# A hash of an unguessable throwaway value, computed once on first use. Verifying
# against THIS is what gives an unknown name the same cost as a real one. It is
# built with the same parameters as real hashes, so the timing genuinely matches
# rather than approximately matching.
_dummy_hash: str | None = None
_dummy_lock = threading.Lock()


def _get_dummy_hash() -> str:
    """Return (computing once) the hash used for timing-parity verifications.

    Returns:
        An encoded Argon2id hash of a random throwaway secret.

    Why:
        Computed lazily and memoized because building it costs a full Argon2
        hashing operation. It is derived from `secrets.token_hex`, never from a
        constant, so the value it verifies is not knowable — nothing can present
        "the dummy password" and match it.
    """
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            import secrets

            _dummy_hash = _hasher().hash(secrets.token_hex(32))
        return _dummy_hash


def hash_password(password: str) -> str:
    """Hash a password for storage.

    Args:
        password: The plaintext password (taken as literal UTF-8 — see Why).

    Returns:
        The encoded Argon2id hash string, which embeds its own salt and parameters.

    Raises:
        PasswordsUnavailable: when argon2-cffi is not installed.
        ValueError: when the password exceeds MAX_PASSWORD_BYTES.

    Why:
        No Unicode normalization is applied, deliberately. Normalizing (NFC/NFKC)
        would let visually identical but differently-encoded strings verify against
        each other, and any normalization choice has to be applied identically here
        and at every future verification site forever — a subtle, permanent coupling.
        Taking bytes literally means "what you typed is what is checked".

        The key pepper is deliberately NOT reused here. Argon2id carries its own
        per-hash salt and its memory-hardness, so peppering adds a second coupled
        secret without adding meaningful strength — and secret independence (the
        session key, the key pepper, and the admin token are already independent)
        is a property worth keeping.
    """
    _check_length(password)
    return _hasher().hash(password)


def _check_length(password: str) -> None:
    """Reject an over-long password before any hashing work happens.

    Args:
        password: The candidate plaintext.

    Raises:
        ValueError: when the UTF-8 encoding exceeds MAX_PASSWORD_BYTES.

    Why:
        Measured in BYTES, not characters: the limit exists to bound work, and
        multi-byte characters cost multi-byte work. Checked before hashing so an
        oversized input is cheap to refuse.
    """
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password exceeds {MAX_PASSWORD_BYTES} bytes")


def verify_password(stored_hash: str | None, password: str) -> tuple[bool, str | None]:
    """Verify a password, ALWAYS doing exactly one Argon2 verification.

    Args:
        stored_hash: The account's encoded Argon2id hash, or None when there is no
            hash to check (unknown account, account without a password, or a
            locked-out account — the caller decides which of those apply).
        password: The presented plaintext.

    Returns:
        (ok, new_hash). `ok` is True only on a genuine match. `new_hash` is a
        freshly computed hash when the stored one used outdated parameters and
        should be rewritten, else None.

    Raises:
        PasswordsUnavailable: when argon2-cffi is not installed.

    Why:
        This function is the timing-parity guarantee. Passing `stored_hash=None`
        makes it verify against the dummy hash and return False — the same work,
        the same duration, as checking a real account's wrong password. So an
        attacker cannot distinguish "no such account" from "wrong password" by
        timing, which is what makes the generic 401 above it actually generic.

        An over-long password and a malformed stored hash both return False rather
        than raising, so neither becomes a 500 that leaks the failure class. The
        malformed case is reported through `new_hash=None` plus the caller's log
        line: a corrupt hash is an operator problem, not a user-visible one.

        The semaphore bounds concurrent memory, and is released in `finally` so a
        raising verification can never leak a slot and deadlock future logins.
    """
    hasher = _hasher()
    try:
        _check_length(password)
    except ValueError:
        # Still burn one verification: returning early here would make an
        # over-long password measurably faster than a normal wrong one.
        password = ""
        stored_hash = None

    target = stored_hash if stored_hash is not None else _get_dummy_hash()
    real = stored_hash is not None

    with _verification_slots:
        try:
            hasher.verify(target, password)
        except Exception:
            # Covers VerifyMismatchError (wrong password), InvalidHash (a corrupt
            # or non-argon2 value in the column), and the dummy-hash mismatch. All
            # are the same answer to the caller: not authenticated.
            return False, None

        if not real:
            # The dummy hash matched, which cannot happen for a real credential —
            # its input is an unguessable random value. Fail closed regardless.
            return False, None

        try:
            if hasher.check_needs_rehash(target):
                return True, hasher.hash(password)
        except Exception:
            # A rehash failure must never turn a SUCCESSFUL login into an error.
            pass
        return True, None
