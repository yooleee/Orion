# =============================================================================
# tests/test_schedule.py
# -----------------------------------------------------------------------------
# Responsible for: The Phase 4 unattended-send safety contract — that the human
#                  preview is bypassed ONLY when BOTH `--yes` (the flag) and
#                  `auto_send = true` (the per-project config) are present, and
#                  that redaction still fires on the auto-send path.
# Role in project: These are the load-bearing privacy tests for scheduled runs.
#                  cli._run_report is the gate; if any of these regress, Orion
#                  could deliver a report no human ever saw — or, worse, leak a
#                  secret on the unattended path. They mirror test_cli.py's
#                  end-to-end style (real repo + state; mocked LLM + network) and
#                  reuse its conftest helpers/fixture (DRY).
# =============================================================================

from orion import cli

# Shared end-to-end setup lives in conftest.py: a real one-commit repo, a config
# writer (here exercised with its auto_send option), and the env_and_mocks fixture
# that mocks the LLM + delivery and captures what WOULD be sent. The fixture is
# auto-discovered by pytest; the plain helpers are imported explicitly.
from conftest import _make_repo, _write_config, use_summary


def _write_multi_config(tmp_path, specs):
    """Write an orion.toml with several projects for `report --all` tests.

    Args:
        tmp_path: per-test temp dir (also where the shared state db lives).
        specs: list of (project_name, repo_path, auto_send_bool) tuples — one per
            project to define.

    Returns:
        Path to the written orion.toml.

    Why:
        The conftest writer defines a single project; the --all tests need a
        registry of several with differing auto_send so we can prove only the
        opted-in ones deliver. Every project routes to the same Discord webhook
        env var (set by env_and_mocks), so all captured sends land in one list and
        a test can simply count them.
    """
    lines = ['state_db = "state.sqlite3"', ""]
    for name, repo, auto_send in specs:
        lines += [
            f"[projects.{name}]",
            f'repo_path = "{repo}"',
            'share_level = "high_level"',
            'collectors = ["git"]',
            f"auto_send = {str(auto_send).lower()}",  # TOML booleans are lowercase
            "",
            f"  [[projects.{name}.recipients]]",
            '  name = "Alex"',
            '  channel = "discord"',
            '  webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"',
            "",
        ]
    toml = tmp_path / "orion.toml"
    toml.write_text("\n".join(lines) + "\n")
    return toml


def _input_must_not_be_called(monkeypatch):
    """Replace input() with a tripwire that fails the test if it is ever called.

    Args:
        monkeypatch: pytest's monkeypatch fixture.

    Returns:
        None. Any call to input() during the test now raises AssertionError.

    Why:
        For an unattended run we need POSITIVE proof the preview prompt never
        happens — not just "nothing was sent." A tripwire is stronger than
        counting calls: if any code path reaches input() (now or after a future
        refactor), the test fails loudly instead of silently blocking forever on a
        real prompt during a scheduled run.
    """

    def boom(prompt=""):
        raise AssertionError("input() was called during an unattended run")

    monkeypatch.setattr("builtins.input", boom)


def _input_spy(monkeypatch, answer):
    """Replace input() with a spy that records its calls and returns `answer`.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        answer: the string the fake input() returns each time ("y" to confirm).

    Returns:
        A list that accumulates one entry per input() call, so a test can assert
        the preview prompt actually happened.

    Why:
        The mirror of the tripwire: when a preview IS required (auto_send set but
        no --yes), we must prove the prompt was shown. Recording calls lets the
        test assert input() was reached at least once.
    """
    calls: list[str] = []

    def fake_input(prompt=""):
        calls.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return calls


def test_yes_and_auto_send_delivers_without_prompt_and_still_redacts(tmp_path, env_and_mocks):
    """--yes + auto_send=true sends with NO preview, and a leaked key is redacted.

    Why this matters: this is the whole point of Phase 4 — an opted-in project can
    be delivered unattended. But "unattended" must never mean "unredacted": even
    on the preview-less path, redaction pass 2 must scrub a secret before it goes
    out. We prove both halves at once — input() is never called, AND a key the
    (mocked) model leaks does not reach the captured send.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)

    # Simulate the model leaking an AWS key into its summary on the auto-send path.
    leak = "Progress, and oops the key AKIAIOSFODNN7EXAMPLE slipped in."
    use_summary(mp, leak)

    # Tripwire: if the preview prompt is reached at all, the test fails.
    _input_must_not_be_called(mp)

    code = cli.main(["report", "demo", "--config", str(toml), "--yes"])
    assert code == 0

    sent = env_and_mocks["sent"]
    assert len(sent) == 1  # delivered without a human in the loop
    message, _ = sent[0]
    assert "AKIAIOSFODNN7EXAMPLE" not in message  # redaction held under auto-send


def test_yes_without_auto_send_is_skipped_not_sent(tmp_path, env_and_mocks):
    """--yes on a project WITHOUT auto_send is skipped — nothing is sent.

    Why this matters: --yes alone must never deliver. A project that has not opted
    in is skipped (and logged), not sent, so adding --yes to a scheduler command
    can't accidentally start blasting reports for every project. Exit is 0 because
    a skip is an intended, non-error outcome (cron should not alarm on it).
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=False)

    # A skip must short-circuit before the preview too: input() must not be called.
    _input_must_not_be_called(mp)

    code = cli.main(["report", "demo", "--config", str(toml), "--yes"])
    assert code == 0
    assert env_and_mocks["sent"] == []  # skipped, not delivered


def test_auto_send_without_yes_still_previews(tmp_path, env_and_mocks):
    """auto_send=true WITHOUT --yes still shows the preview (input() IS called).

    Why this matters: THIS is the load-bearing safety test. Config alone must
    never bypass the human gate — when a human runs the command (no --yes), the
    preview happens regardless of auto_send, because a person is present to look.
    We prove the prompt was actually reached, then (having answered "y") that the
    send went through the normal confirmed path.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)

    # Spy that confirms ("y") and records that the prompt was shown.
    calls = _input_spy(mp, "y")

    code = cli.main(["report", "demo", "--config", str(toml)])  # NOTE: no --yes
    assert code == 0
    assert len(calls) >= 1  # the preview prompt was shown despite auto_send=true
    assert len(env_and_mocks["sent"]) == 1  # and the confirmed send went out


def test_all_yes_delivers_only_auto_send_projects(tmp_path, env_and_mocks):
    """`report --all --yes` delivers the auto_send projects and skips the rest.

    Why this matters: this is the scheduled-digest entry point. Across a registry
    of projects, only those that opted in (auto_send=true) may be delivered
    unattended; the others are skipped, never sent — even though --yes is global.
    With two opted-in and one opted-out (all with real activity), exactly two
    sends should result, and no preview should ever be shown.
    """
    mp = env_and_mocks["monkeypatch"]
    specs = [
        ("alpha", _make_repo(tmp_path, name="alpha"), True),
        ("beta", _make_repo(tmp_path, name="beta"), False),
        ("gamma", _make_repo(tmp_path, name="gamma"), True),
    ]
    toml = _write_multi_config(tmp_path, specs)

    # No preview anywhere: opted-in projects auto-send, the opted-out one is
    # skipped before any prompt.
    _input_must_not_be_called(mp)

    code = cli.main(["report", "--all", "--config", str(toml), "--yes"])
    assert code == 0  # no failures -> exit 0
    assert len(env_and_mocks["sent"]) == 2  # alpha + gamma sent; beta skipped


def test_all_is_fail_soft_and_exits_nonzero_on_a_real_failure(tmp_path, env_and_mocks):
    """One project failing does not stop the others, and the run exits non-zero.

    Why this matters: a scheduled --all run must be robust — a single broken
    project (here, a repo_path that doesn't exist -> GitError) should be reported
    as FAILED but must not prevent the healthy project from delivering. The exit
    code is non-zero so cron surfaces the real failure, while the good project
    still got its report out.
    """
    mp = env_and_mocks["monkeypatch"]
    good_repo = _make_repo(tmp_path, name="good")
    missing_repo = tmp_path / "does_not_exist"  # never created -> git collect fails
    specs = [
        ("broken", missing_repo, True),   # auto_send so it proceeds to collection
        ("healthy", good_repo, True),
    ]
    toml = _write_multi_config(tmp_path, specs)

    _input_must_not_be_called(mp)

    code = cli.main(["report", "--all", "--config", str(toml), "--yes"])
    assert code == 1  # a genuine FAILED -> non-zero exit
    assert len(env_and_mocks["sent"]) == 1  # the healthy project still delivered


def test_all_skips_only_exits_zero(tmp_path, env_and_mocks):
    """An --all --yes run where every project is opted-out is a clean exit 0.

    Why this matters: "nothing was eligible to send" is a routine, intended
    outcome, not a failure. If skips returned non-zero, a scheduler would alarm on
    a perfectly normal run, training the user to ignore alerts. Only real failures
    should be non-zero.
    """
    mp = env_and_mocks["monkeypatch"]
    specs = [
        ("alpha", _make_repo(tmp_path, name="alpha"), False),
        ("beta", _make_repo(tmp_path, name="beta"), False),
    ]
    toml = _write_multi_config(tmp_path, specs)

    _input_must_not_be_called(mp)

    code = cli.main(["report", "--all", "--config", str(toml), "--yes"])
    assert code == 0  # only skips -> exit 0
    assert env_and_mocks["sent"] == []  # nothing delivered


def test_report_requires_a_project_or_all(tmp_path, env_and_mocks):
    """`report` with neither a project nor --all is a usage error (exit 2).

    Why this matters: the two ways to run report are mutually exclusive and one is
    required; omitting both should fail fast with a clear message, not silently do
    nothing.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)

    code = cli.main(["report", "--config", str(toml)])
    assert code == 2
    assert env_and_mocks["sent"] == []


def test_report_rejects_project_and_all_together(tmp_path, env_and_mocks):
    """`report <project> --all` is a usage error (exit 2).

    Why this matters: passing both is ambiguous (one project, or all of them?), so
    it must be rejected rather than guessed at.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)

    code = cli.main(["report", "demo", "--all", "--config", str(toml), "--yes"])
    assert code == 2
    assert env_and_mocks["sent"] == []
