# =============================================================================
# tests/test_isolation.py
# -----------------------------------------------------------------------------
# Responsible for: Pinning the conftest test-isolation guards so they cannot
#                  silently regress — (1) no real external network leaves the test
#                  process, and (2) the real project .env / secret env vars never
#                  bleed into a test.
# Role in project: These guards are the structural reason a test can never leak a
#                  real secret. If a refactor disabled one, secrets could start
#                  flowing again; these tests fail loudly the moment that happens.
# =============================================================================

import os
import socket
import urllib.error
import urllib.request

import pytest


def test_network_guard_blocks_a_non_loopback_connect():
    """A direct socket connect to a non-loopback host is refused by the guard.

    Why this matters: this is the security boundary. The connect is refused before any
    byte is sent (no packet actually leaves), so a forgotten sender mock can never
    transmit a real secret. We dial an IP literal (no DNS) so nothing resolves either.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="network guard"):
            sock.connect(("93.184.216.34", 80))  # an external IP; never actually reached
    finally:
        sock.close()


def test_network_guard_allows_loopback():
    """Loopback connects still work — the relay's real-server tests depend on this.

    Why this matters: the guard must block ONLY external hosts; a 127.0.0.1 connection
    (what the relay tests make to their own ThreadingHTTPServer) has to keep working.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("127.0.0.1", port))  # must NOT raise
    finally:
        client.close()
        server.close()


def test_urllib_to_an_external_host_is_blocked():
    """urllib (the senders' actual HTTP client) cannot reach a real host either.

    Why this matters: the senders POST via urllib, so the guard must cover that path,
    not just raw sockets. We use an IP literal to avoid a real DNS lookup; the blocked
    connect surfaces as a urllib error.
    """
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen("http://93.184.216.34/", timeout=2)


def test_real_secrets_do_not_bleed_into_the_environment():
    """The real project .env's credentials are absent from os.environ during a test.

    Why this matters: pins the env-isolation half. A test that does not set these must
    never observe a real value (an API key, a webhook, a relay token) carried in from
    the project .env or the shell.
    """
    for var in (
        "ORION_RELAY_VIEW_TOKEN",
        "ORION_RELAY_TOKEN",
        "ORION_RELAY_ADMIN_TOKEN",
        "ANTHROPIC_API_KEY",
        "ORION_SLACK_WEBHOOK_SAM",
    ):
        assert os.environ.get(var) is None, f"{var} bled into the test environment"


def test_load_secrets_does_not_load_the_real_env_in_a_normal_test():
    """Calling load_secrets in an unmarked test does NOT load the real project .env.

    Why this matters: the no-op of load_dotenv is what stops a mid-test re-bleed (a test
    that calls load_secrets would otherwise pull the real .env back into os.environ). We
    call it and confirm a known real-.env var stays unset.
    """
    from orion.secrets import load_secrets

    load_secrets()  # unguarded, this would load the real .env found by CWD search
    assert os.environ.get("ORION_RELAY_VIEW_TOKEN") is None
