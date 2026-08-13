# =============================================================================
# scripts/redaction_baseline.py
# -----------------------------------------------------------------------------
# Responsible for: Measuring what the redaction preview warning actually shows an
#                  operator at the production 400-line diff cap, and how each
#                  candidate exemption class would change it. This is the
#                  instrument behind KI-41's close-out (2026-08-13) and the
#                  re-measurement command named by KI-47's revisit trigger.
# Role in project: Dev-only instrument. Run by hand from the repo root; never
#                  imported by shipped code. It imports the SHIPPED redactor and
#                  collector filters so the measurement cannot drift from real
#                  behavior.
# Assumptions: Run from the repo root (or pass --config). orion.toml and its
#              state_db exist locally; the configured repo_paths are present.
#              Read-only by construction: the state DB is opened mode=ro and
#              every git call is a read (log / diff / rev-list / rev-parse).
#
# OUTPUT IS MASKED. This script's output is meant to be read in LLM sessions,
# and the windows it scans are real diffs that may contain real secrets. It
# prints aggregates by default; --show-samples prints hit names with values
# reduced to a 2-character prefix plus length. Do not "improve" it to print
# raw values.
#
# Method (recorded so a re-run measures the same thing):
# - One "window" = one collector report body, assembled exactly as
#   collectors.git.collect() assembles it (commits + diffstat + sensitive/
#   noise-filtered diff, capped at _DIFF_LINE_CAP), with an end-sha substituted
#   for HEAD. That substitution is the only divergence from collect().
# - For every project in orion.toml with rows in report_history, window
#   boundaries are the REAL report timestamps (the sha at each sent_at), so the
#   replay shows what the operator would have seen at the actual cadence had
#   share_level been "detailed". A final window covers last-report -> HEAD.
# - Every other git repo under --repos-root gets one window per active day
#   (hypothetical cadence, stated as such).
# - Each window is scored with the shipped redact() (the preview's hit count),
#   and its catch-all hits are classified by the heuristics below to estimate
#   what each exemption class could remove.
#
# The classification heuristics are MEASUREMENT-ONLY and deliberately live here,
# not in src/orion/redact.py: the shipped classifier redacts everything, and
# KI-41's close-out decided it stays that way.
# =============================================================================
"""Re-baseline the redaction preview distribution at the production diff cap.

Usage, from the repo root::

    python scripts/redaction_baseline.py [--config orion.toml]
        [--repos-root ~/Developer] [--show-samples]

Prints the per-preview warning distribution (the number an operator sees), the
catch-all hit split by class, and the counterfactual distribution under each
candidate exemption class. See KI-41 / KI-47 in docs/known-issues.md for how
these numbers were used.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import statistics
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

# The script lives in scripts/; the package lives in src/. Resolved relative to
# this file so the script works no matter the current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orion.collectors.git import _DIFF_LINE_CAP, _is_noise, _is_sensitive  # noqa: E402
from orion.redact import (  # noqa: E402
    _CREDENTIAL_KEYWORDS,
    _PATTERNS,
    _SECRET_ASSIGNMENT,
    redact,
)

# git's well-known empty-tree object: diffing against it yields a repo's full
# history as one diff, which is what a first-ever collector run reports.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git(repo: Path, *args: str) -> str:
    """Run a read-only git command and return stdout ('' on failure).

    Args:
        repo: Repository to run in (via -C, so no cwd changes).
        *args: The git subcommand and its arguments.

    Returns:
        Captured stdout, or the empty string if git exited non-zero.

    Why:
        The instrument must never abort a whole measurement because one repo has
        an odd state; an empty section is visible in the counts, a crash is not.
        Failures are swallowed to '' exactly like an empty diff would be.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


def _load_reported_projects(config_path: Path) -> tuple[dict[str, Path], Path]:
    """Read orion.toml and return {project name: repo_path} plus the state DB path.

    Args:
        config_path: Path to orion.toml.

    Returns:
        A (projects, state_db_path) pair. Projects maps every configured project
        to its repo_path; state_db is resolved relative to the config file, the
        same rule the CLI uses.

    Why:
        Boundaries come from report_history, which is keyed by project name, so
        the name->repo mapping must come from the same config the reports were
        produced under — hardcoding repo paths here would drift.
    """
    with config_path.open("rb") as f:
        config = tomllib.load(f)
    projects = {
        name: Path(stanza["repo_path"])
        for name, stanza in config.get("projects", {}).items()
        if "repo_path" in stanza
    }
    state_db = Path(config.get("state_db", "orion.sqlite3"))
    if not state_db.is_absolute():
        state_db = config_path.parent / state_db
    return projects, state_db


def _real_report_boundaries(repo: Path, sent_ats: list[str]) -> list[str]:
    """Map real report timestamps to commit boundaries, ending at current HEAD.

    Args:
        repo: The project's repository.
        sent_ats: ISO timestamps from report_history, ascending.

    Returns:
        Deduplicated boundary shas: the commit that was HEAD at each report
        time, plus today's HEAD as a final boundary.

    Why:
        Replaying the actual cadence is the honest version of "what does the
        operator see" — synthetic windowing was exactly the metric error
        (aggregate counts, cap lifted) that misled the 2026-08-06 attempt.
    """
    boundaries: list[str] = []
    for ts in sent_ats:
        sha = _git(repo, "rev-list", "-1", f"--before={ts}", "HEAD").strip()
        if sha and (not boundaries or boundaries[-1] != sha):
            boundaries.append(sha)
    head = _git(repo, "rev-parse", "HEAD").strip()
    if head and (not boundaries or boundaries[-1] != head):
        boundaries.append(head)
    return boundaries


def _day_boundaries(repo: Path) -> list[str]:
    """One boundary per active day: the last commit of each distinct commit date.

    Args:
        repo: The repository to scan.

    Returns:
        Shas in chronological order, one per day that has at least one commit.

    Why:
        Repos that were never reported have no real cadence to replay. One
        window per active day approximates "reported at the end of each working
        day", which matches the observed cadence of the projects that WERE
        reported (orion: 57 reports over ~58 days).
    """
    last_sha_by_day: dict[str, str] = {}
    day_order: list[str] = []
    for line in _git(repo, "log", "--reverse", "--pretty=%H %cI").splitlines():
        sha, committed = line.split(" ", 1)
        day = committed[:10]
        if day not in last_sha_by_day:
            day_order.append(day)
        last_sha_by_day[day] = sha
    return [last_sha_by_day[day] for day in day_order]


def _build_window(repo: Path, base: str | None, end: str, detailed: bool) -> tuple[str, bool]:
    """Assemble one collector report body for the base..end range.

    Args:
        repo: The repository.
        base: The previous boundary sha, or None for a first-ever window
            (diffed against the empty tree, like collect() with since_sha=None).
        end: The boundary sha standing in for HEAD.
        detailed: True mirrors share_level="detailed" (capped, filtered diff);
            False mirrors "high_level" (no diff section).

    Returns:
        (window text, has_activity) — has_activity False means collect() would
        have produced no report, so the window is excluded from the counts.

    Why:
        This mirrors collectors.git.collect() section-for-section (commits,
        diffstat, filtered capped diff) rather than calling it, because
        collect() can only diff against the repo's CURRENT HEAD and a replay
        needs historical end points. The end-sha substitution is this
        instrument's one divergence from the shipped path; every filter and cap
        is imported, not copied.
    """
    log_range = [f"{base}..{end}"] if base else [end]
    commits = _git(
        repo, "log", *log_range, "--pretty=format:- %h %s (%an, %ad)", "--date=short"
    ).strip()
    diff_base = base or _EMPTY_TREE
    diffstat = _git(repo, "diff", "--stat", diff_base, end).strip()

    sections = [
        f"# Git activity for {repo.name}",
        "",
        "## Commits",
        commits or "(no commit messages)",
        "",
        "## Files changed",
        diffstat or "(no file changes)",
    ]
    if detailed:
        changed = [
            line
            for line in _git(repo, "diff", "--name-only", diff_base, end).splitlines()
            if line.strip()
        ]
        allowed = [p for p in changed if not _is_sensitive(p) and not _is_noise(p)]
        excluded = [p for p in changed if p not in allowed]
        diff = _git(repo, "diff", diff_base, end, "--", *allowed) if allowed else ""
        lines = diff.splitlines()
        if len(lines) > _DIFF_LINE_CAP:
            lines = lines[:_DIFF_LINE_CAP] + [
                f"... [diff truncated: {len(lines) - _DIFF_LINE_CAP} more lines omitted] ..."
            ]
        if diff:
            sections += ["", "## Diff (excerpt)", "\n".join(lines)]
        if excluded:
            # collect() names held-back files (never their contents); replicated
            # so the window is section-for-section identical to a real report.
            sections += [
                "",
                "## Omitted from diff (sensitive or noise files)",
                ", ".join(sorted(excluded)),
            ]
    return "\n".join(sections).strip(), bool(commits or diffstat)


# --- Measurement-only hit classification -------------------------------------
#
# The credential keywords are REGEX FRAGMENTS (api[_\-]?key), not literal words.
# A literal-string containment check therefore misclassifies ANTHROPIC_API_KEY
# as carrying no delimited keyword — a bug found and fixed while building this
# instrument, and the reason each fragment is compiled with letter-boundary
# lookarounds here instead of searched with str.find().
_DELIMITED_KEYWORDS = [
    re.compile(rf"(?<![a-zA-Z])(?:{kw})(?![a-zA-Z])", re.IGNORECASE)
    for kw in _CREDENTIAL_KEYWORDS
]

# Mirrors the extractor's optional quote + scheme-word prefix so the classified
# "value" is the credential position, not the word Bearer.
_VALUE_PREFIX = re.compile(r"^['\"]?(?:(?:Bearer|Basic)[ \t]+)?['\"]?", re.IGNORECASE)


def _bare_value(value: str) -> str:
    """Strip quotes and trailing punctuation that ride along in diff lines.

    Args:
        value: The raw matched value token.

    Returns:
        The value without surrounding quotes or trailing , ; . punctuation.

    Why:
        In real diffs benign values arrive as `false,` or `"true",` — without
        this strip the literal class undercounts (measured: 5 literal hits
        classified opaque on the first instrument iteration).
    """
    return value.strip("'\"").rstrip(",;.").strip("'\"")


def _is_prose_name(name: str) -> bool:
    """True if no credential keyword appears delimiter-bounded in the name.

    Args:
        name: The extractor's group(1), e.g. "authenticated" or "AUTH_TOKEN".

    Returns:
        True when every keyword occurrence is embedded inside a longer
        alphabetic word (author, authenticated) — KI-41's class.

    Why:
        This is the name-side question KI-41 asked. It is a measurement label,
        not an exemption: the close-out measured that exempting this class
        soundly is not worth building.
    """
    return not any(p.search(name) for p in _DELIMITED_KEYWORDS)


def _is_benign_value(value: str) -> bool:
    """True for value shapes that are safe to emit verbatim by construction.

    Args:
        value: The matched value with any quote/scheme prefix removed.

    Returns:
        True for exact non-secret literals (true/false/null/none/nil), strict
        placeholders (<token>, {token}, ${VAR}, ****), and version pins
        (=1.2.3 — the second '=' of a pip '==' pin lands in the value).

    Why:
        These are the shapes for which "the whole span is safe" is arguable
        from the value alone — the only sound exemption shape the Unit 2
        analysis found. Measured yield: 20 of 106 hits, 2 of 31 warned
        previews cleaned, which is why it was not built.
    """
    v = _bare_value(value)
    if v.lower() in {"true", "false", "null", "none", "nil"}:
        return True
    if re.fullmatch(
        r"<[A-Za-z][A-Za-z0-9 _.\-]*>"  # <token>, <your-key-here>
        r"|\{\{?[A-Za-z][A-Za-z0-9_.\-]*\}\}?"  # {token}, {{secret}}
        r"|\$\{?[A-Z][A-Z0-9_]*\}?"  # $VAR, ${VAR}
        r"|[x*•]{4,}",  # xxxx, ****
        v,
    ):
        return True
    return re.fullmatch(r"=?v?\d+(\.\d+)+", v) is not None


def _is_code_value(value: str) -> bool:
    """True for values that read as source code rather than an opaque secret.

    Args:
        value: The matched value with any quote/scheme prefix removed.

    Returns:
        True when the value contains expression punctuation — KI-47's class
        (auth=_admin_auth()), token: str, etc.).

    Why:
        KI-47's question is whether these could be exempted. Unlike
        _is_benign_value this is NOT sound by construction (an unquoted secret
        passed as a function argument would ride out inside the span), which is
        why it is measured here and deliberately not shipped.
    """
    return not _is_benign_value(value) and any(c in value for c in "(){}[],;")


def _catch_all_hits(window: str) -> list[dict[str, object]]:
    """Extract and classify the catch-all's hits for one window.

    Args:
        window: The window text (detailed variant).

    Returns:
        One dict per catch-all match: name, value, and the three class flags.

    Why:
        Replicates redact()'s order — the seven specific patterns first, then
        the catch-all on the survivor text — so the classified matches are
        exactly the ones the shipped catch-all redacts, no more and no fewer.
    """
    text = window
    for pattern, replacement in _PATTERNS:
        text, _ = pattern.subn(replacement, text)
    hits = []
    for m in _SECRET_ASSIGNMENT.finditer(text):
        # The value is the span minus name, separator, and the optional
        # quote/scheme decoration — the same slice a span-aware classifier
        # would judge (KI-41 close-out, "the seam answer").
        value = _VALUE_PREFIX.sub("", text[m.end(2) : m.end(0)])
        hits.append(
            {
                "name": m.group(1),
                "value": value,
                "prose": _is_prose_name(m.group(1)),
                "benign": _is_benign_value(value),
                "code": _is_code_value(value),
            }
        )
    return hits


def _distribution(counts: list[int]) -> str:
    """Format the per-preview distribution the way the KI records it.

    Args:
        counts: One preview hit count per active window.

    Returns:
        A one-line summary: warned windows, median, max, total hits.

    Why:
        The per-preview distribution IS the decision metric — aggregate hit
        counts are not operator experience (the 2026-08-06 lesson).
    """
    warned = sum(1 for c in counts if c > 0)
    return (
        f"warn {warned:3d}/{len(counts)} ({100 * warned / len(counts):3.0f}%)  "
        f"median {statistics.median(counts):g}  max {max(counts):3d}  "
        f"hits {sum(counts):4d}"
    )


def main() -> None:
    """Build the windows, score them, and print the counterfactual table.

    Returns:
        None. Output goes to stdout, aggregates only unless --show-samples.

    Why:
        One entry point so the revisit trigger in KI-47 is literally one
        command: python scripts/redaction_baseline.py
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("orion.toml"))
    parser.add_argument(
        "--repos-root",
        type=Path,
        default=Path.home() / "Developer",
        help="Directory scanned for additional (never-reported) git repos, "
        "measured at one window per active day.",
    )
    parser.add_argument(
        "--show-samples",
        action="store_true",
        help="Print per-hit names with masked values (2-char prefix + length).",
    )
    args = parser.parse_args()

    projects, state_db = _load_reported_projects(args.config)
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    sent_ats: dict[str, list[str]] = {}
    for project, ts in con.execute(
        "SELECT project, sent_at FROM report_history ORDER BY sent_at"
    ):
        sent_ats.setdefault(project, []).append(ts)
    con.close()

    # (repo, cadence, base, end) — real-cadence windows for reported projects,
    # day-cadence for everything else under --repos-root.
    windows: list[tuple[Path, str, str | None, str]] = []
    for project, repo in projects.items():
        previous = None
        for sha in _real_report_boundaries(repo, sent_ats.get(project, [])):
            windows.append((repo, "real", previous, sha))
            previous = sha
    # Dedupe by (device, inode), NOT by resolved path: Path.resolve() does not
    # case-normalize, so on macOS's case-insensitive filesystem a config path of
    # ".../applications" fails to match the on-disk ".../Applications" and the
    # repo gets measured twice (a real bug this instrument shipped with, caught
    # by the verifier pass — the duplicate inflated the window count to 111).
    reported_ids = set()
    for p in projects.values():
        try:
            stat = p.stat()
            reported_ids.add((stat.st_dev, stat.st_ino))
        except OSError:
            pass
    if args.repos_root.is_dir():
        for candidate in sorted(args.repos_root.iterdir()):
            if not (candidate / ".git").is_dir():
                continue
            stat = candidate.stat()
            if (stat.st_dev, stat.st_ino) in reported_ids:
                continue
            previous = None
            for sha in _day_boundaries(candidate):
                windows.append((candidate, "day", previous, sha))
                previous = sha

    scored = []  # (repo name, cadence, detailed hits, high_level hits, hit dicts)
    for repo, cadence, base, end in windows:
        detailed, active = _build_window(repo, base, end, detailed=True)
        if not active:
            continue
        high_level, _ = _build_window(repo, base, end, detailed=False)
        scored.append(
            (
                repo.name,
                cadence,
                redact(detailed).hit_count,
                redact(high_level).hit_count,
                _catch_all_hits(detailed),
            )
        )

    detailed_counts = [row[2] for row in scored]
    print(f"active windows: {len(scored)} "
          f"(real-cadence {sum(1 for r in scored if r[1] == 'real')}, "
          f"day-cadence {sum(1 for r in scored if r[1] == 'day')})")
    print(f"BASELINE detailed   : {_distribution(detailed_counts)}")
    print(f"BASELINE high_level : {_distribution([row[3] for row in scored])}")

    counterfactuals = [
        ("minus prose-name (UNSOUND — leaks, see KI-41)", lambda h: h["prose"]),
        ("minus benign-value (sound)", lambda h: h["benign"]),
        ("minus prose-name AND benign-value (sound)", lambda h: h["prose"] and h["benign"]),
        ("minus code-value (KI-47, not sound by construction)", lambda h: h["code"]),
        ("minus benign-value + code-value", lambda h: h["benign"] or h["code"]),
    ]
    for label, predicate in counterfactuals:
        remaining = [
            row[2] - sum(1 for h in row[4] if predicate(h)) for row in scored
        ]
        cleaned = sum(
            1 for row, left in zip(scored, remaining) if row[2] > 0 and left == 0
        )
        print(f"{label:52s}: {_distribution(remaining)}  previews-cleaned {cleaned}")

    all_hits = [h for row in scored for h in row[4]]
    print(
        f"\ncatch-all hits {len(all_hits)}: "
        f"prose-name {sum(1 for h in all_hits if h['prose'])}, "
        f"benign-value {sum(1 for h in all_hits if h['benign'])}, "
        f"code-value {sum(1 for h in all_hits if h['code'])}"
    )
    print("warned windows by repo:",
          dict(Counter(row[0] for row in scored if row[2] > 0)))

    if args.show_samples:
        print("\nhits, masked (class flags | name = value[:2]…(len)):")
        seen = set()
        for h in all_hits:
            flags = "".join(
                flag for flag, on in (("P", h["prose"]), ("B", h["benign"]), ("C", h["code"])) if on
            ) or "-"
            key = (flags, h["name"], str(h["value"])[:2], len(str(h["value"])))
            if key not in seen:
                seen.add(key)
                print(f"  [{flags:2s}] {h['name']} = {str(h['value'])[:2]}…({len(str(h['value']))} ch)")


if __name__ == "__main__":
    main()
