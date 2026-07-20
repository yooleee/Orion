# =============================================================================
# tests/test_relay_passwords.py
# -----------------------------------------------------------------------------
# Responsible for: The Argon2id hashing helper — round-trip, the parity contract,
#                  input handling, and the concurrency bound.
# Role in project: Unit-level cover for relay/passwords.py. The server tests prove
#                  login works end to end; these pin the properties that are the
#                  reason this module exists separately at all.
# Assumptions: argon2-cffi is installed (the `relay` extra). Tests that must run
#              without it monkeypatch the import path instead of uninstalling.
# =============================================================================
"""Unit tests for Argon2id password hashing (auth revamp, Unit 3)."""

import threading

import pytest

from relay import passwords
from relay.passwords import (
    MAX_PASSWORD_BYTES,
    PasswordsUnavailable,
    hash_password,
    verify_password,
)


def test_a_password_round_trips_and_a_wrong_one_does_not():
    """The basic contract: the right password verifies, a wrong one does not."""
    stored = hash_password("correct horse battery staple")
    assert verify_password(stored, "correct horse battery staple")[0] is True
    assert verify_password(stored, "correct horse battery stapl")[0] is False


def test_the_same_password_hashes_differently_every_time():
    """Two hashes of one password differ, and both verify.

    Why this matters: each hash carries its own random salt. Identical hashes would mean no
    salt, which would let one precomputed table crack every account at once and would reveal
    which accounts share a password.
    """
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password(a, "same-password")[0] is True
    assert verify_password(b, "same-password")[0] is True


def test_the_encoded_hash_records_the_parameters_it_used():
    """The stored hash is self-describing Argon2id.

    Why this matters: parameters live IN the hash, which is what allows them to be raised
    later without invalidating existing passwords — an old hash keeps verifying under its own
    recorded cost, and check_needs_rehash spots it for upgrade on next login.
    """
    stored = hash_password("whatever")
    assert stored.startswith("$argon2id$")
    assert f"m={passwords._MEMORY_COST_KIB}" in stored
    assert f"t={passwords._TIME_COST}" in stored


def test_verifying_against_no_stored_hash_returns_false_not_an_error():
    """A None stored hash verifies against the dummy and cleanly fails.

    Why this matters: this is the timing-parity path. The caller passes None for an unknown
    account, an account with no password, and a locked-out one — all three must cost a real
    verification and return a plain False, never raise.
    """
    assert verify_password(None, "anything")[0] is False


def test_the_dummy_hash_cannot_be_matched():
    """No input authenticates against the parity path.

    Why this matters: the dummy exists to burn time, and it is built from a random secret
    precisely so nothing can present "the dummy password" and be let in. Even if something
    could, the real=False guard refuses. Both belts are checked here.
    """
    dummy = passwords._get_dummy_hash()
    assert verify_password(None, dummy)[0] is False
    assert verify_password(None, "")[0] is False


def test_a_malformed_stored_hash_is_false_not_an_exception():
    """Garbage in the verifier column denies the login without raising.

    Why this matters: the server must return a normal 401 for this, not a 500. A crash would
    both leak that this particular account is special and turn a data problem into an outage.
    """
    assert verify_password("not-an-argon2-hash-at-all", "anything")[0] is False
    assert verify_password("", "anything")[0] is False


def test_an_over_length_password_is_rejected_on_hash_and_false_on_verify():
    """Oversized input is refused when hashing and simply fails when verifying.

    Why this matters: an unbounded input is free work handed to an attacker. Hashing raises so
    an admin sees the problem immediately; verifying returns False so the login path stays on
    its single generic-failure route rather than erroring differently for long inputs.
    """
    too_long = "x" * (MAX_PASSWORD_BYTES + 1)
    with pytest.raises(ValueError):
        hash_password(too_long)

    stored = hash_password("short-one")
    assert verify_password(stored, too_long)[0] is False


def test_the_length_limit_counts_bytes_not_characters():
    """A multi-byte password is measured by its UTF-8 length.

    Why this matters: the limit exists to bound work, and multi-byte characters cost multi-byte
    work. Counting characters would let a 3-byte-per-char passphrase cost three times the cap.
    """
    # Each of these is 3 bytes in UTF-8, so just over a third of the cap in characters is over.
    over = "あ" * (MAX_PASSWORD_BYTES // 3 + 1)
    assert len(over) < MAX_PASSWORD_BYTES        # under the limit by CHARACTER count
    with pytest.raises(ValueError):              # but over it by bytes
        hash_password(over)


def test_unicode_is_taken_literally_without_normalization():
    """Two Unicode spellings of the same glyph are different passwords.

    Why this matters: a deliberate choice. Normalizing (NFC/NFKC) would make visually identical
    but differently-encoded strings interchangeable, and the choice would have to be applied
    identically at every future verification site forever. Taking bytes literally means what
    you typed is what is checked.
    """
    composed = "café"      # e + combining acute
    precomposed = "café"    # single é
    assert composed != precomposed
    stored = hash_password(precomposed)
    assert verify_password(stored, composed)[0] is False


def test_hashing_fails_closed_without_the_dependency(monkeypatch):
    """Without argon2-cffi, hashing raises PasswordsUnavailable rather than degrading.

    Why this matters: the fail-closed invariant. Falling back to a weaker scheme would be an
    invisible security downgrade; the named error tells the operator exactly what is missing.
    """
    def no_argon2():
        raise PasswordsUnavailable("argon2-cffi is not installed")

    monkeypatch.setattr(passwords, "_hasher", no_argon2)
    with pytest.raises(PasswordsUnavailable):
        hash_password("anything")


def test_concurrent_verifications_are_bounded():
    """No more than the cap run at once, and every caller still completes.

    Why this matters: Argon2 is memory-hard and the relay spawns an unbounded thread per
    request, so without this bound a burst of login attempts allocates memory linearly with
    concurrency and can OOM-kill the VM — an unauthenticated denial of service. The bound must
    also not deadlock or drop callers: excess requests wait, they do not fail.
    """
    stored = hash_password("the-password")
    peak = 0
    live = 0
    lock = threading.Lock()
    results = []

    real_semaphore = passwords._verification_slots

    class CountingSemaphore:
        """Wraps the real semaphore to observe how many verifications overlap."""

        def __enter__(self):
            nonlocal peak, live
            real_semaphore.acquire()
            with lock:
                live += 1
                peak = max(peak, live)
            return self

        def __exit__(self, *exc):
            nonlocal live
            with lock:
                live -= 1
            real_semaphore.release()
            return False

    passwords._verification_slots = CountingSemaphore()
    try:
        def attempt():
            results.append(verify_password(stored, "the-password")[0])

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        passwords._verification_slots = real_semaphore

    assert results == [True] * 12                              # nobody was dropped
    assert peak <= passwords._MAX_CONCURRENT_VERIFICATIONS     # and memory stayed bounded
