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

from datetime import datetime, timedelta, timezone
from pathlib import Path

from orion import cli
from orion.config import ProjectConfig
from orion.state import open_state, record_report

# Shared end-to-end setup lives in conftest.py: a real one-commit repo, a config
# writer (here exercised with its auto_send option), and the env_and_mocks fixture
# that mocks the LLM + delivery and captures what WOULD be sent. The fixture is
# auto-discovered by pytest; the plain helpers are imported explicitly.
from conftest import _make_repo, _write_config, use_summary


def _write_multi_config(tmp_path, specs, cadences=None):
    """Write an orion.toml with several projects for `report --all` tests.

    Args:
        tmp_path: per-test temp dir (also where the shared state db lives).
        specs: list of (project_name, repo_path, auto_send_bool) tuples — one per
            project to define.
        cadences: optional {project_name: cadence_str} map; when a name is present,
            a `cadence = "..."` line is emitted for it. Additive and defaulted None
            so the existing auto_send-only callers are unchanged — only the E1.2
            `--due` tests pass it.

    Returns:
        Path to the written orion.toml.

    Why:
        The conftest writer defines a single project; the --all tests need a
        registry of several with differing auto_send so we can prove only the
        opted-in ones deliver. Every project routes to the same Discord webhook
        env var (set by env_and_mocks), so all captured sends land in one list and
        a test can simply count them. The `--due` tests reuse this exact registry,
        just adding a per-project cadence.
    """
    cadences = cadences or {}
    lines = ['state_db = "state.sqlite3"', ""]
    for name, repo, auto_send in specs:
        lines += [
            f"[projects.{name}]",
            # as_posix(): forward slashes so a Windows repo path isn't read as a
            # TOML escape sequence (see conftest._write_config for the full why).
            f'repo_path = "{repo.as_posix()}"',
            'share_level = "high_level"',
            'collectors = ["git"]',
            f"auto_send = {str(auto_send).lower()}",  # TOML booleans are lowercase
        ]
        if name in cadences:
            lines.append(f'cadence = "{cadences[name]}"')
        lines += [
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


# =============================================================================
# E1.2 — `report --all --due`: the cadence-aware filter (closes KI-13)
# -----------------------------------------------------------------------------
# Two layers of coverage:
#   - cli._is_due unit tests pin the core decision (the two always-due cases and
#     the slack-adjusted UTC interval) deterministically, by injecting `now`.
#   - end-to-end `--all --due` tests prove the filter skips a recently-reported
#     project, reconciles the tally, and never bypasses the preview gate.
# =============================================================================


def _bare_project(name, cadence):
    """Build a minimal ProjectConfig for the _is_due unit tests.

    Args:
        name: the project key (also the report_history key).
        cadence: the cadence preset ("daily"/"weekly") or None.

    Returns:
        A ProjectConfig carrying just what _is_due reads (name + cadence). It never
        collects git here, so a placeholder repo_path is fine.

    Why:
        _is_due only reads project.name and project.cadence and queries the state
        conn — no pipeline runs — so constructing the dataclass directly is the
        smallest, clearest fixture (no config file, no repo needed).
    """
    return ProjectConfig(
        name=name,
        repo_path=Path("/tmp/unused"),
        share_level="high_level",
        collectors=("git",),
        recipients=(),
        cadence=cadence,
    )


def _seed_last_report(conn, name, when):
    """Record a report_history row so get_last_report_time(name) == when.

    Args:
        conn: an open state connection.
        name: the project key.
        when: a timezone-aware datetime for the delivered timestamp.

    Why:
        `--due` reads the last-sent time from report_history (Decision 2 — no new
        schema). Seeding a row is how we place a project inside or outside its
        cadence window without running a whole report first.
    """
    record_report(conn, name, "seeded body", ["Alex"], when.isoformat(timespec="seconds"))


def test_is_due_no_cadence_is_always_due(tmp_path):
    """A project with no cadence is due even if it just reported.

    Why this matters: `--due` is opt-in — an un-cadenced project must behave exactly
    like plain --all (always due), so existing configs are unaffected by the flag.
    """
    now = datetime.now(timezone.utc)
    conn = open_state(tmp_path / "state.sqlite3")
    _seed_last_report(conn, "demo", now)  # reported this instant...
    # ...yet with no cadence set it is still due.
    assert cli._is_due(_bare_project("demo", None), conn, now) is True


def test_is_due_never_reported_is_due(tmp_path):
    """A cadenced project with no history row is due (nothing to be too-soon after).

    Why this matters: a brand-new or never-delivered project must not be silently
    skipped forever; with no last-sent time it is due, and will still exit
    NO_ACTIVITY cheaply if there is nothing new.
    """
    now = datetime.now(timezone.utc)
    conn = open_state(tmp_path / "state.sqlite3")
    assert cli._is_due(_bare_project("demo", "daily"), conn, now) is True


def test_not_due_within_daily_interval(tmp_path):
    """daily cadence, reported 2h ago → NOT due (inside the 23h min interval).

    Why this matters: this is the whole point — a project reported very recently is
    skipped so a scheduled --all --due doesn't re-report it every run.
    """
    now = datetime.now(timezone.utc)
    conn = open_state(tmp_path / "state.sqlite3")
    _seed_last_report(conn, "demo", now - timedelta(hours=2))
    assert cli._is_due(_bare_project("demo", "daily"), conn, now) is False


def test_due_after_daily_interval(tmp_path):
    """daily cadence, reported 24h ago → due (past the 23h interval).

    Why this matters: the interval is 23h (a full day minus ~1h DST/jitter slack), so a
    daily scheduler firing once a day always lets the next daily report through, without
    the interval being so loose it shortens the cadence. 22h would still be too soon (a
    sub-daily rerun is skipped); 24h is due.
    """
    now = datetime.now(timezone.utc)
    conn = open_state(tmp_path / "state.sqlite3")
    _seed_last_report(conn, "demo", now - timedelta(hours=22))
    assert cli._is_due(_bare_project("demo", "daily"), conn, now) is False  # 22h < 23h
    conn2 = open_state(tmp_path / "later.sqlite3")
    _seed_last_report(conn2, "demo", now - timedelta(hours=24))
    assert cli._is_due(_bare_project("demo", "daily"), conn2, now) is True  # 24h ≥ 23h


def test_weekly_interval_boundary(tmp_path):
    """weekly cadence: 6 days ago → not due; 7 days ago → due.

    Why this matters: pins the weekly preset's interval at 6d23h — tight enough that a
    daily scheduler delivers a "weekly" project on day 7, NOT day 6 (which a looser 6d
    interval would cause, drifting it faster than weekly). Checked on both sides.
    """
    now = datetime.now(timezone.utc)
    # Separate state dbs per direction: get_last_report_time is MAX(sent_at), so a
    # single db can't hold two different "last report" times — the max would win.
    inside = open_state(tmp_path / "inside.sqlite3")
    _seed_last_report(inside, "demo", now - timedelta(days=6))   # 6d < 6d23h → day-6 skip
    assert cli._is_due(_bare_project("demo", "weekly"), inside, now) is False
    past = open_state(tmp_path / "past.sqlite3")
    _seed_last_report(past, "demo", now - timedelta(days=7))     # 7d ≥ 6d23h → day-7 send
    assert cli._is_due(_bare_project("demo", "weekly"), past, now) is True


def test_is_due_treats_malformed_timestamp_as_due(tmp_path):
    """A garbage last-report timestamp makes the project due, not a crash.

    Why this matters: _is_due runs in the --all loop AHEAD of _run_report's per-project
    fail-soft, so an unparseable history row (external tampering, a format change) must
    not abort the whole run. Reporting the project is the conservative fallback — a report
    is safer than silently going quiet on a bad row.
    """
    now = datetime.now(timezone.utc)
    conn = open_state(tmp_path / "state.sqlite3")
    # Seed a row whose sent_at is not valid ISO 8601 (simulates a corrupted/legacy row).
    _seed_last_report(conn, "demo", now)  # a normal row first...
    conn.execute("UPDATE report_history SET sent_at = ? WHERE project = ?", ("not-a-date", "demo"))
    conn.commit()
    assert cli._is_due(_bare_project("demo", "daily"), conn, now) is True


def test_is_due_treats_future_timestamp_as_due(tmp_path):
    """A last-report time AHEAD of now makes the project due, not suppressed.

    Why this matters: a clock correction or imported state can leave a future timestamp;
    a naive `now - last >= interval` would go negative and wrongly suppress the project
    until that future time plus its cadence. Treating "last reported in the future" as due
    keeps a skewed clock from silently muting a project.
    """
    now = datetime.now(timezone.utc)
    conn = open_state(tmp_path / "state.sqlite3")
    _seed_last_report(conn, "demo", now + timedelta(days=3))  # reported "in the future"
    assert cli._is_due(_bare_project("demo", "daily"), conn, now) is True


def test_all_due_reports_only_the_stale_project(tmp_path, env_and_mocks, capsys):
    """`--all --due --yes` skips a fresh project and reports the stale one; tally reconciles.

    Why this matters: this is the end-to-end payoff — one scheduled entry over a
    mixed-cadence registry reports only what is actually due. `fresh` (daily,
    reported 2h ago) is skipped NOT_DUE before any collection; `stale` (daily,
    reported 2 days ago) still has git activity and is delivered. Exit is 0 (a skip
    is routine) and the summary counts both projects (numbers reconcile).
    """
    mp = env_and_mocks["monkeypatch"]
    specs = [
        ("fresh", _make_repo(tmp_path, name="fresh"), True),
        ("stale", _make_repo(tmp_path, name="stale"), True),
    ]
    toml = _write_multi_config(
        tmp_path, specs, cadences={"fresh": "daily", "stale": "daily"}
    )

    # Pre-seed the last-report times into the SAME state db the run will open.
    now = datetime.now(timezone.utc)
    conn = open_state(tmp_path / "state.sqlite3")
    _seed_last_report(conn, "fresh", now - timedelta(hours=2))   # inside 23h → skip
    _seed_last_report(conn, "stale", now - timedelta(days=2))    # past 23h → due
    conn.close()

    _input_must_not_be_called(mp)  # auto_send + --yes: no preview for the due one

    code = cli.main(["report", "--all", "--due", "--config", str(toml), "--yes"])
    assert code == 0
    # Only the stale project was collected and delivered.
    assert len(env_and_mocks["sent"]) == 1
    out = capsys.readouterr().out
    assert "not due yet (cadence=daily)" in out          # fresh was announced-skipped
    # The tally counts BOTH projects (1 sent + 1 not due) — nothing silently dropped.
    assert "2 project(s):" in out
    assert "1 sent" in out
    assert "1 not due" in out


def test_due_without_all_is_a_usage_error(tmp_path, env_and_mocks):
    """`report <project> --due` (no --all) is a usage error (exit 2), nothing sent.

    Why this matters: --due filters the --all set; on a single named project it has
    no meaning, so it is rejected rather than silently ignored.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)

    code = cli.main(["report", "demo", "--due", "--config", str(toml), "--yes"])
    assert code == 2
    assert env_and_mocks["sent"] == []


def test_due_without_yes_still_previews_the_due_project(tmp_path, env_and_mocks):
    """A due project under `--due` (no --yes) still shows the preview before sending.

    Why this matters: the --due filter runs BEFORE, and must never bypass, the
    preview gate. A due project with a human present (no --yes) must still preview —
    proving the filter changed which projects run, not how they are gated.
    """
    mp = env_and_mocks["monkeypatch"]
    specs = [("demo", _make_repo(tmp_path, name="demo"), True)]
    toml = _write_multi_config(tmp_path, specs, cadences={"demo": "daily"})
    # No history row → the project is due. No --yes → the preview must be shown.
    calls = _input_spy(mp, "y")

    code = cli.main(["report", "--all", "--due", "--config", str(toml)])  # no --yes
    assert code == 0
    assert len(calls) >= 1                       # the preview prompt was shown
    assert len(env_and_mocks["sent"]) == 1       # and the confirmed send went out
