# =============================================================================
# tests/conftest.py
# -----------------------------------------------------------------------------
# Responsible for: Shared test helpers and fixtures for the CLI-level tests
#                  (test_cli.py and test_schedule.py), which both drive the full
#                  report pipeline against a REAL temporary git repo with only the
#                  LLM call and the network POST mocked.
# Role in project: Keeps the end-to-end setup (a throwaway repo, a config writer,
#                  the env-var + LLM + delivery mocks, and the scripted preview
#                  answer) in ONE place so each test file states only what it
#                  cares about (DRY). pytest auto-discovers conftest.py, so the
#                  `env_and_mocks` fixture is available to every test without an
#                  import; the plain helper functions are imported explicitly
#                  (`from conftest import _make_repo, ...`).
# =============================================================================

import os
import socket
import subprocess

import pytest

from orion import cli
from orion.compose import _render_preview

# =============================================================================
# Test isolation: no real network, no real .env bleeding into the test process
# -----------------------------------------------------------------------------
# Two session/function-wide guards make the suite leak-proof BY CONSTRUCTION rather
# than by every test remembering to mock its sender:
#   1. _block_external_network — any non-loopback socket connect raises, so no test
#      can ever transmit a real secret (a real webhook / Anthropic / relay call). The
#      relay's own tests connect only to 127.0.0.1, which stays allowed.
#   2. _isolate_secrets — neutralizes orion.secrets.load_dotenv so the real project
#      .env (which python-dotenv would find by searching up from the CWD) never loads
#      into os.environ, and scrubs any Orion/Anthropic secret already present. Tests
#      set exactly the env they need via monkeypatch.setenv; the few that genuinely
#      exercise real .env discovery opt out with @pytest.mark.real_dotenv.
# Background: the real .env was bleeding into os.environ during tests (e.g. a leaked
# ORION_RELAY_VIEW_TOKEN once broke an unrelated test). The network guard is the actual
# security boundary; the env scrub is hygiene + test correctness.
# =============================================================================

# The secret env-var prefixes Orion uses. Any var with these prefixes is a credential
# (API key, webhook URL, relay token) that must never be inherited from the real
# environment into a test — tests set what they need explicitly.
_SECRET_ENV_PREFIXES = ("ORION_", "ANTHROPIC_")

_REAL_SOCKET_CONNECT = socket.socket.connect


def _is_loopback_address(address) -> bool:
    """True when a socket address targets loopback (always allowed in tests).

    Args:
        address: The address passed to socket.connect — a (host, port) tuple for
            AF_INET/AF_INET6, or a str/other for AF_UNIX etc.

    Returns:
        True for loopback hosts (127.0.0.0/8, ::1, localhost) and non-IP addresses
        (e.g. AF_UNIX paths, which never reach the external network); False otherwise.

    Why:
        The relay tests run a real ThreadingHTTPServer on 127.0.0.1 and connect to it,
        so loopback must work. Anything else is a real external host — the thing we
        block. Non-tuple addresses can't be an external TCP target, so we allow them
        rather than crash on an unexpected shape.
    """
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    if host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
        return True
    return isinstance(host, str) and host.startswith("127.")


def _loopback_only_connect(self, address):
    """A socket.connect replacement that blocks any non-loopback target.

    Why:
        This is the structural security guarantee: even a future test that forgets to
        mock its sender cannot transmit a real secret, because the connection to the
        external host is refused before any bytes are sent. A clear OSError points the
        author at the fix (mock the sender / HTTP client).
    """
    if _is_loopback_address(address):
        return _REAL_SOCKET_CONNECT(self, address)
    raise OSError(
        f"Test network guard blocked an outbound connection to {address!r}. "
        "Tests must not make real external calls — mock the sender or HTTP client."
    )


@pytest.fixture(autouse=True, scope="session")
def _block_external_network():
    """Block every non-loopback socket connection for the whole test session.

    Why:
        Makes leaking a real secret impossible by construction. urllib/http.client all
        funnel through socket.connect, so patching it once covers every sender. The
        full suite makes zero non-loopback connections (verified), so this changes no
        behavior — it only fails closed if a real call is ever introduced.
    """
    socket.socket.connect = _loopback_only_connect
    try:
        yield
    finally:
        socket.socket.connect = _REAL_SOCKET_CONNECT


@pytest.fixture(autouse=True)
def _isolate_secrets(request, monkeypatch):
    """Stop the real .env / environment secrets from reaching tests.

    Args:
        request: pytest's request, used to honor the `real_dotenv` opt-out marker.
        monkeypatch: function-scoped, so every change is undone after each test.

    Why:
        orion.secrets.load_secrets calls python-dotenv's load_dotenv(), which searches
        up from the CWD and finds the REAL project .env, loading its credentials into
        os.environ for the rest of the process (load_dotenv mutates os.environ directly,
        so it isn't auto-undone). We no-op that seam so a normal run never loads the real
        .env, and we scrub any Orion/Anthropic secret already present so a value bled by
        an earlier opted-out test (or exported in the shell) can't leak in either. Tests
        that genuinely exercise real .env discovery (test_secrets.py) mark themselves
        `real_dotenv` to keep the real loader.
    """
    # Scrub pre-existing secrets first (runs before env_and_mocks and the test body, so
    # anything a test sets afterward still wins).
    for key in list(os.environ):
        if key.startswith(_SECRET_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    # Neutralize the real .env load unless the test opted out.
    if request.node.get_closest_marker("real_dotenv") is None:
        monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)


def pytest_configure(config):
    """Register the `real_dotenv` marker (opt out of the load_dotenv no-op).

    Why:
        A handful of tests in test_secrets.py verify load_secrets' real .env discovery,
        so they need the genuine python-dotenv loader. Declaring the marker here keeps
        `pytest --strict-markers` clean and documents the escape hatch.
    """
    config.addinivalue_line(
        "markers",
        "real_dotenv: use the real python-dotenv loader (opt out of the conftest no-op).",
    )


def _run(repo, *args):
    """Run a git command in a test repo, raising on failure.

    Args:
        repo: Path to the git repo to run the command in.
        *args: The git subcommand and its arguments.

    Returns:
        None. Raises CalledProcessError if git exits non-zero.

    Why:
        Tests build real repos so the git collector runs for real; this wrapper
        keeps each git call to one readable line and fails loudly on a bad setup.
    """
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path, name="repo"):
    """Create a git repo with one commit and return its path.

    Args:
        tmp_path: pytest's per-test temporary directory.
        name: subdirectory name for the repo. Defaults to "repo" so existing
            single-repo callers are unchanged; the `report --all` tests pass
            distinct names to build several repos under one tmp_path.

    Returns:
        Path to the initialized repo containing a single commit.

    Why:
        A real one-commit repo gives the git collector genuine activity to find on
        the first run, so the pipeline has something to report end to end.
    """
    repo = tmp_path / name
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "t@example.com")
    _run(repo, "config", "user.name", "Tester")
    (repo / "feature.py").write_text("def f():\n    return 1\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "Add feature")
    return repo


def _write_config(tmp_path, repo, *, auto_send=None):
    """Write an orion.toml in tmp_path pointing at the repo. Returns its path.

    Args:
        tmp_path: per-test temp dir (also where the state db lives).
        repo: path to the git repo (used by the git collector).
        auto_send: when not None, emit `auto_send = true/false` for the project.
            Omitted by default so existing callers get the config's own default
            (False), and so Phase 4 tests can set it explicitly either way.

    Returns:
        Path to the written orion.toml.

    Why:
        A single shared writer means test_cli.py and test_schedule.py describe the
        same minimal project the same way; the optional auto_send keeps the Phase 4
        opt-in expressible without a second near-identical writer (DRY).
    """
    auto_send_line = ""
    if auto_send is not None:
        # TOML booleans are lowercase; render the Python bool accordingly.
        auto_send_line = f"auto_send = {str(auto_send).lower()}\n"
    toml = tmp_path / "orion.toml"
    # repo.as_posix() writes the path with forward slashes. A Windows path like
    # C:\\Users\\... placed in a DOUBLE-QUOTED TOML string is read as escape
    # sequences (\\U, \\x ... expect hex), which fails to parse ("Invalid hex
    # value") — exactly the gotcha orion.toml.example warns users about. Forward
    # slashes parse on every OS, and Path()/git both accept them on Windows, so the
    # fixture's config is valid cross-platform (caught by CI on the Windows runner).
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [projects.demo]
        repo_path = "{repo.as_posix()}"
        share_level = "high_level"
        collectors = ["git"]
        {auto_send_line}
          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


def _payload_text(payload):
    """Extract the human-readable text from a delivery payload.

    Args:
        payload: The JSON body compose handed to a sender — a Discord embed, a
            Slack Block Kit message, or a plain {"content"/"text": …} fallback.

    Returns:
        The text content of the payload as one string.

    Why:
        Delivery POSTs a dict payload, not a bare string. The integration tests
        care about WHAT TEXT reached WHICH webhook, not the JSON envelope (the
        envelope shape is pinned in test_delivery / test_slack_delivery and
        test_report_compose). We reuse the production preview renderer so the test
        view of "what was sent" matches exactly what the user is shown at preview.
    """
    return _render_preview(payload)


def _answer(monkeypatch, value):
    """Script the preview confirm prompt to return `value`.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        value: the string the (replaced) input() should return ("y" / "n").

    Returns:
        None. Replaces builtins.input for the duration of the test.

    Why:
        The preview gate calls input(); scripting it lets a test confirm ("y") or
        decline ("n") deterministically with no terminal. test_schedule.py instead
        replaces input() with a call-counting spy to PROVE it is or isn't called.
    """
    monkeypatch.setattr("builtins.input", lambda prompt="": value)


class _FakeSummarizer:
    """A stand-in Summarizer (B4): summarize() returns a canned body.

    Args:
        body: The summary string to return for any input.

    Why:
        B4 moved the summarizer behind a Summarizer Protocol that
        cli._build_summarizer constructs from config. Tests patch that builder to
        return this fake, so no Anthropic client or API key is built and the test
        controls the report body — the same role the old cli.summarize_raw
        monkeypatch played, just one level up at the seam.
    """

    def __init__(self, body: str = "Made progress."):
        self._body = body

    def summarize(self, text: str, share_level: str) -> str:
        return self._body


def use_summary(monkeypatch, body: str = "Made progress.") -> None:
    """Patch cli._build_summarizer to return a _FakeSummarizer(body).

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        body: The canned summary the fake returns (a test can inject a string
            with a secret to prove redaction still runs on the LLM output).

    Returns:
        None. Side effect: the CLI builds the fake summarizer on the raw lane.

    Why:
        Keeps the summarizer patch target in ONE place (DRY): every CLI test that
        used to do `mp.setattr(cli, "summarize_raw", lambda ...)` now calls this,
        so a future seam change touches conftest, not a dozen tests.
    """
    monkeypatch.setattr(
        cli, "_build_summarizer", lambda cfg, secret_getter: _FakeSummarizer(body)
    )


@pytest.fixture
def env_and_mocks(monkeypatch):
    """Set required env vars and capture mocked LLM + delivery.

    Args:
        monkeypatch: pytest's monkeypatch fixture (also returned for further use).

    Returns:
        A dict with a `sent` list that records (message, url) tuples the (mocked)
        Discord sender was asked to deliver, plus the `monkeypatch` handle.

    Why:
        Centralizes the three mocks every CLI test needs — the Anthropic + webhook
        env vars, the LLM call, and the network POST — so each test only scripts
        the preview answer and asserts on outcomes (DRY). Nothing real leaves the
        machine: delivery is captured in memory.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")

    sent: list[tuple[str, str]] = []

    # Default summary echoes nothing sensitive; individual tests can override via
    # use_summary(). Patches the seam (cli._build_summarizer), so no Anthropic
    # client/key is built and the canned body is returned on the raw lane.
    use_summary(monkeypatch)
    # Record the delivered text (extracted from the payload dict) keyed to its
    # webhook, so tests assert on the message string as before.
    monkeypatch.setattr(
        cli, "discord_send", lambda payload, url: sent.append((_payload_text(payload), url))
    )

    return {"sent": sent, "monkeypatch": monkeypatch}
