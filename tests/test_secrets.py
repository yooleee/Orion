# =============================================================================
# tests/test_secrets.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying get_required's present/missing/blank behavior.
# Role in project: Secrets are the one path by which an API key or webhook URL
#                  enters the program; a missing one must fail with a clear name.
# =============================================================================

import pytest

from orion.secrets import SecretsError, get_required


def test_present_secret_is_returned(monkeypatch):
    """A set environment variable is returned, stripped of whitespace.

    Why this matters: trailing newlines in a copy-pasted .env value are common;
    they would silently break a webhook URL if not stripped.
    """
    monkeypatch.setenv("ORION_TEST_SECRET", "  value123  ")
    assert get_required("ORION_TEST_SECRET") == "value123"


def test_missing_secret_raises_with_name(monkeypatch):
    """An unset variable raises SecretsError naming the exact variable.

    Why this matters: the error should tell the user precisely which line to add
    to .env, not surface later as an opaque auth failure.
    """
    monkeypatch.delenv("ORION_TEST_SECRET", raising=False)
    with pytest.raises(SecretsError, match="ORION_TEST_SECRET"):
        get_required("ORION_TEST_SECRET")


def test_blank_secret_treated_as_missing(monkeypatch):
    """A whitespace-only value is treated as missing.

    Why this matters: a blank line like `KEY=` in .env is a forgotten value, not
    an intentional empty secret; failing here is safer than sending an empty key.
    """
    monkeypatch.setenv("ORION_TEST_SECRET", "   ")
    with pytest.raises(SecretsError):
        get_required("ORION_TEST_SECRET")
