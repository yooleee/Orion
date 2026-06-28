# =============================================================================
# collectors/git.py
# -----------------------------------------------------------------------------
# Responsible for: The RAW-LANE collector. Reads a local git repo and returns
#                  "what changed since the last report" as text for the summarizer.
# Role in project: Step 1 of the report pipeline. Produces a CollectorResult that
#                  is redacted, then (because lane == raw) summarized by the LLM.
# How it reads git: shells out via subprocess to `git` — zero dependency, and we
#                  only need read-side commands (log / diff / rev-parse).
# SECURITY: Two independent filters protect the diff. (1) A path DENYLIST excludes
#                  known-sensitive files (.env, keys, credentials) from the diff
#                  ENTIRELY — their contents are never read. Exclusion is computed
#                  in Python and the surviving paths are passed explicitly to
#                  `git diff`, so the diff can only contain affirmatively-allowed
#                  files. (2) share_level "high_level" omits the diff altogether.
#                  Diffstat (filenames + line counts, no content) is always safe.
# Assumptions: `git` is on PATH. repo_path points at a local clone.
# =============================================================================

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from orion.collectors import LANE_RAW, CollectorResult

# Git's well-known empty-tree object. Diffing against it on a first run yields the
# entire history as "added," with no special-casing needed.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Hard cap on diff size (lines). A privacy AND cost control: it bounds how much
# raw code can ever reach the LLM, regardless of how large a change is.
_DIFF_LINE_CAP = 400

# SECURITY DENYLIST: files whose CONTENT must never enter the diff. Matched
# against both the full path and the basename (so credentials in any subdir are
# caught). This is the collection-time "don't even read the secret" layer.
_SENSITIVE_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.keystore",
    "*.jks",
    "credentials*",
    "*secret*",
    "*.secrets",
)

# NOISE LIST: files that are not secret but add bulk and no narrative value
# (lockfiles, minified bundles, sourcemaps). Excluded from the diff to keep the
# summary focused and the token cost down. Distinct from the security denylist.
_NOISE_GLOBS = (
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "Pipfile.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
)


class GitError(Exception):
    """Raised when a git command fails or the path is not a usable repo.

    Why:
        Lets the CLI tell a setup problem (bad repo_path, history rewritten) apart
        from a bug, and abort the run cleanly without sending anything.
    """


def _git(repo_path: Path, *args: str) -> str:
    """Run a git command in repo_path and return its stdout.

    Args:
        repo_path: The repository to run in (passed via `git -C`).
        *args: The git subcommand and its arguments.

    Returns:
        Captured stdout, decoded as UTF-8 (with surrogate-escape for odd bytes).

    Why:
        One wrapper means every git call gets identical error handling and
        decoding. We use `-C repo_path` rather than changing the process's cwd so
        the collector has no global side effects (important if Orion later reports
        on several repos in one run). errors="replace" keeps a stray non-UTF-8
        byte in a diff from crashing the whole run.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError as exc:  # `git` not installed / not on PATH.
        raise GitError("git executable not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        # git writes the human-readable reason to stderr; surface it verbatim.
        raise GitError(
            f"git {' '.join(args)} failed:\n{exc.stderr.strip()}"
        ) from exc
    return completed.stdout


def _ensure_repo(repo_path: Path) -> None:
    """Validate that repo_path exists and is inside a git work tree.

    Args:
        repo_path: The configured repository path.

    Returns:
        None. Raises GitError if the path is missing or not a git repo.

    Why:
        Checking up front turns two common misconfigurations (typo'd path, pointed
        at a non-repo) into one clear message instead of a confusing failure deep
        in a later git call.
    """
    if not repo_path.exists():
        raise GitError(f"Repository path does not exist: {repo_path}")
    # `rev-parse --is-inside-work-tree` prints "true" inside a repo, else errors.
    _git(repo_path, "rev-parse", "--is-inside-work-tree")


def _head_sha(repo_path: Path) -> str | None:
    """Return the current HEAD commit sha, or None for a repo with no commits.

    Args:
        repo_path: The repository path (already validated).

    Returns:
        The 40-char HEAD sha, or None if the repo has no commits yet.

    Why:
        HEAD is both the delta marker and the activity signal. An empty repo
        (initialized but never committed) has no HEAD; we map that to None so the
        caller can treat it as "no activity" rather than an error.
    """
    try:
        # --verify fails cleanly if HEAD doesn't resolve (no commits yet).
        return _git(repo_path, "rev-parse", "--verify", "HEAD").strip()
    except GitError:
        return None


def _is_sensitive(path: str) -> bool:
    """Return True if a changed path matches the security denylist.

    Args:
        path: A repo-relative file path from `git diff --name-only`.

    Returns:
        True if the file's content must be kept out of the diff.

    Why:
        Matching both the full path and the basename means a sensitive file is
        caught wherever it lives in the tree (e.g. `config/prod/.env`).
    """
    name = Path(path).name
    return any(
        fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(name, glob)
        for glob in _SENSITIVE_GLOBS
    )


def _is_noise(path: str) -> bool:
    """Return True if a changed path is low-value noise (lockfiles, bundles).

    Args:
        path: A repo-relative file path.

    Returns:
        True if the file should be omitted from the diff for focus/cost (not for
        security).

    Why:
        Same basename/path matching as _is_sensitive, kept separate so the two
        reasons for exclusion (security vs noise) stay distinct and auditable.
    """
    name = Path(path).name
    return any(
        fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(name, glob)
        for glob in _NOISE_GLOBS
    )


def _changed_files(repo_path: Path, base: str) -> list[str]:
    """List files changed between base and HEAD.

    Args:
        repo_path: The repository path.
        base: The commit (or empty-tree sha) to diff from.

    Returns:
        Repo-relative paths of changed files (may be empty).

    Why:
        We need the file list in Python to classify it (sensitive/noise/normal)
        before deciding which paths to feed to `git diff`. --name-only gives just
        the paths with no content, so this step itself never exposes a secret.
    """
    out = _git(repo_path, "diff", "--name-only", base, "HEAD")
    return [line for line in out.splitlines() if line.strip()]


def _build_diff(repo_path: Path, base: str, allowed_files: list[str]) -> str:
    """Build a capped unified diff limited to an explicit allowlist of files.

    Args:
        repo_path: The repository path.
        base: The commit (or empty-tree sha) to diff from.
        allowed_files: The exact, already-filtered paths to include.

    Returns:
        The diff text, truncated to _DIFF_LINE_CAP lines with a marker if longer.
        Empty string if there are no allowed files.

    Why:
        Passing the explicit surviving paths after `--` (not a glob) guarantees
        the diff contains only files we affirmatively allowed — a sensitive file
        cannot slip in through a mis-written exclusion pattern. The line cap
        bounds both token cost and how much code can ever reach the LLM.
    """
    if not allowed_files:
        return ""

    # `--` separates revisions from pathspecs; each path is a literal argv item,
    # so paths with spaces are handled correctly.
    diff = _git(repo_path, "diff", base, "HEAD", "--", *allowed_files)

    lines = diff.splitlines()
    if len(lines) > _DIFF_LINE_CAP:
        kept = lines[:_DIFF_LINE_CAP]
        kept.append(
            f"... [diff truncated: {len(lines) - _DIFF_LINE_CAP} more lines omitted] ..."
        )
        return "\n".join(kept)
    return diff


def collect(repo_path: Path, since_sha: str | None, share_level: str) -> CollectorResult:
    """Collect git activity since the last reported commit.

    Args:
        repo_path: Local path to the git repository (from config).
        since_sha: The last reported HEAD sha, or None for a first-ever run.
        share_level: "high_level" (no code diff) or "detailed" (capped diff).

    Returns:
        A CollectorResult on the raw lane. has_activity is False (and raw_text
        empty) when there is nothing new since since_sha.

    Why:
        This is the whole raw lane in one call. The hybrid payload — commit
        messages (intent) + diffstat (scope) + an optional capped diff (detail) —
        gives the summarizer enough to narrate well while the filters and cap keep
        secrets and bulk out. Activity is detected by comparing HEAD to since_sha,
        which is robust and also handles the empty-repo and no-change cases.
    """
    _ensure_repo(repo_path)

    head = _head_sha(repo_path)
    if head is None:
        # Repo exists but has no commits yet — nothing to report.
        return CollectorResult(lane=LANE_RAW, raw_text="", new_marker="", has_activity=False)

    if since_sha is not None and head == since_sha:
        # Already reported up to the current HEAD — no new activity.
        return CollectorResult(lane=LANE_RAW, raw_text="", new_marker=head, has_activity=False)

    # base is the previous marker, or the empty tree for a first-ever run.
    base = since_sha if since_sha is not None else _EMPTY_TREE
    # On an incremental run, restrict the commit log to the new range; on a first
    # run, list all history (no range argument).
    log_range = [f"{since_sha}..HEAD"] if since_sha is not None else ["HEAD"]

    # Commit messages: intent, cheap and high-signal.
    commits = _git(
        repo_path,
        "log",
        *log_range,
        "--pretty=format:- %h %s (%an, %ad)",
        "--date=short",
    ).strip()

    # Diffstat: scope (which files, how much). Safe for all files — counts only,
    # never content — so sensitive files may appear here by NAME, which is useful
    # ("updated .env") and not a leak.
    diffstat = _git(repo_path, "diff", "--stat", base, "HEAD").strip()

    sections = [
        f"# Git activity for {repo_path.name}",
        "",
        "## Commits",
        commits or "(no commit messages)",
        "",
        "## Files changed",
        diffstat or "(no file changes)",
    ]

    # The capped code diff is the only part gated by share_level. high_level omits
    # it entirely; detailed includes it for the allowed (non-sensitive, non-noise)
    # files only.
    if share_level == "detailed":
        changed = _changed_files(repo_path, base)
        allowed = [p for p in changed if not _is_sensitive(p) and not _is_noise(p)]
        excluded = [p for p in changed if p not in allowed]
        diff_text = _build_diff(repo_path, base, allowed)
        if diff_text:
            sections += ["", "## Diff (excerpt)", diff_text]
        if excluded:
            # Note WHICH files were held back, so the omission is visible (no
            # silent truncation), without revealing their contents.
            sections += [
                "",
                "## Omitted from diff (sensitive or noise files)",
                ", ".join(sorted(excluded)),
            ]

    raw_text = "\n".join(sections).strip()
    return CollectorResult(
        lane=LANE_RAW, raw_text=raw_text, new_marker=head, has_activity=True
    )


def tracked_files(repo_path: Path) -> list[str]:
    """List the repo-relative paths of all version-controlled files.

    Args:
        repo_path: Local path to the git repository (already a valid repo, or
            validated here).

    Returns:
        Repo-relative paths of every tracked file (may be empty for a repo with no
        commits / nothing staged). Forward-slash separators as git emits them, so the
        result is stable across Windows/macOS/Linux.

    Why:
        The skills collector (E2 Inc 4 slice 4c) derives a project's *languages* from
        the extensions of its tracked files. `git ls-files` lists exactly what is under
        version control — never build output, vendored deps, or untracked scratch — so
        the language signal reflects the code the developer actually authored. Reading
        through git (not os.walk) reuses the repo boundary git already enforces and
        keeps every git access in this module (single responsibility), matching how
        collect() shells out. Read-only, content-free (paths only), so no secret can
        leak through it.
    """
    _ensure_repo(repo_path)
    out = _git(repo_path, "ls-files")
    return [line for line in out.splitlines() if line.strip()]


def recent_subjects(repo_path: Path, limit: int = 50) -> list[str]:
    """Return the subject lines of the most recent commits, newest first.

    Args:
        repo_path: Local path to the git repository (validated here).
        limit: Maximum number of commit subjects to return (a bound on cost and on how
            much text reaches the LLM). Defaults to 50.

    Returns:
        Up to `limit` commit subject lines (the first line of each message), newest
        first. Empty for a repo with no commits.

    Why:
        Commit subjects are a cheap, high-signal record of *what work was done* — the
        evidence the skills collector reframes into competencies. We take only the
        subject (`%s`), never the body or any diff, so this stays content-free of code
        and bounded by `limit`. An empty repo (no HEAD) yields no subjects rather than
        an error, mirroring collect()'s empty-repo tolerance.
    """
    _ensure_repo(repo_path)
    if _head_sha(repo_path) is None:
        # No commits yet — no subjects to read. Return empty rather than letting the
        # `git log` below fail on a repo with no HEAD.
        return []
    out = _git(
        repo_path,
        "log",
        f"-n{limit}",
        "--pretty=format:%s",
    )
    return [line for line in out.splitlines() if line.strip()]
