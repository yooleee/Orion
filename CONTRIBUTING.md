# Contributing to Orion

Thanks for your interest in Orion. This is a **local-first, pre-release** project: it
turns your local project activity (git, tasks, notes, pushed updates) into readable
progress updates and delivers them to supervisors over Discord and Slack. Collectors
read your local files; only delivery makes an outbound call; reports are previewed
before anything is sent.

Orion is **pre-release and unversioned** — progress is tracked by *phase*, not by a
version number (see [`plans/orion-plan.md`](plans/orion-plan.md) for the roadmap and
[`CHANGELOG.md`](CHANGELOG.md) for what has shipped). Contributions are welcome; the
guidance below keeps changes small, reviewable, and aligned with the project's goals.

## Before you start

- **Read [`plans/orion-plan.md`](plans/orion-plan.md).** It is the source of truth for
  architecture, the phase order, and decisions already settled. Orion is built strictly
  phase-by-phase, so a change is most reviewable when it fits the current phase rather
  than pulling work forward from a later one.
- **Skim [`docs/known-issues.md`](docs/known-issues.md).** Until the project is hosted,
  this is the local stand-in for an issue tracker — cross-phase bugs, design questions,
  and conscious limitations live there. Your idea may already be recorded with context.
- **For anything non-trivial, open an issue first** so we can agree on the approach
  before you write code. This avoids work that doesn't fit the current phase or the
  project's scope.

## Project values

Orion favors the simplest thing that works:

- **Minimal, justified dependencies.** Orion has **two** runtime dependencies
  (`anthropic`, `python-dotenv`); everything else is the standard library (`tomllib`,
  `sqlite3`, `subprocess`, `urllib`, `pathlib`). The goal is "clone and run in ten
  minutes." Adding a dependency needs a clear justification against that goal.
- **Cross-platform by default.** Orion must run on Linux, macOS, and Windows. Make
  *every* change with cross-compatibility in mind: use `pathlib` over hand-built path
  strings, no `shell=True`, no OS-specific path/shell assumptions, and don't hardcode
  `.venv/bin/` (native Windows uses `.venv\Scripts\`). Where platforms genuinely
  diverge (e.g. scheduling), delegate to the OS's native tool.
- **Privacy is non-negotiable.** See [SECURITY.md](SECURITY.md). Secrets live in a
  gitignored `.env` and are never committed; redaction scrubs secrets before anything
  leaves the machine; preview-before-send is the human gate that makes the guarantee.
- **Smallest reviewable unit.** Prefer several small, focused PRs over one large diff.
  If a change is large, propose a breakdown.

## Development setup

You need **Python 3.11+** (for the stdlib `tomllib` TOML parser) and **`git` on your
PATH**.

```bash
git clone <your-fork-url> orion && cd orion

# Create and activate a virtual environment (activation is the one per-OS step)
python -m venv .venv
source .venv/bin/activate       # macOS / Linux
# .venv\Scripts\activate        # Windows (PowerShell or cmd)

# Install Orion with the dev extra (adds pytest)
python -m pip install -e ".[dev]"
```

Everything after activation uses `python -m ...`, which is identical on every OS.

## Running the tests

```bash
python -m pytest
```

CI runs the full suite on every push and pull request across **{Linux, macOS, Windows}
× {Python 3.11, 3.12, 3.13}** (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
Please make sure `pytest` is green locally before you open a PR; the cross-OS matrix is
where a portability regression would otherwise surface. What the suite covers — and what
is intentionally *not* covered — is documented in [`docs/testing.md`](docs/testing.md).

## Code standards

Orion is written to be read and learned from, so annotation is expected, not optional:

- **Docstrings** on functions and methods, with `Args:`, `Returns:`, and a short
  `Why:` note explaining why the function exists or why the approach was chosen.
- **File-level headers** on new files: what the file is responsible for, how it fits
  into the project, and any important assumptions or dependencies.
- **Inline comments that explain *why*, not *what*** — flag non-obvious choices and
  library-specific behavior, not self-evident lines.
- **Cross-platform-minded code** as described under Project values above.
- **Tests** for any non-trivial function or module, with comments explaining what
  scenario each test covers and why it matters.

## Commit and pull-request style

- **Commit messages** are concise: a short subject line, plus a few bullets in the body
  only when the change isn't self-explanatory from the diff. Do **not** add
  `Co-Authored-By` lines or other attribution trailers.
- **Keep living docs current in the same change.** If your work changes design or
  behavior, update [`plans/orion-plan.md`](plans/orion-plan.md); record shipped work in
  [`CHANGELOG.md`](CHANGELOG.md); record open concerns or conscious limitations in
  [`docs/known-issues.md`](docs/known-issues.md). Docs should never drift from the code.
- **Open a PR** against `main` and fill in the PR template. Link the issue or the plan
  section your change relates to, and run through the checklist.

## Reporting a security issue

Please do **not** open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md)
for how to report one privately.
