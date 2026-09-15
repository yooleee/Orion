# =============================================================================
# secrets.py
# -----------------------------------------------------------------------------
# Responsible for: Loading secrets from a gitignored .env into the environment
#                  and handing them out by name, with a clear error when missing —
#                  and, since CS-O PR9, GENERATING missing relay secrets into that
#                  same .env on explicit request (`relay-serve --init-secrets`).
# Role in project: The only module that reads the Anthropic API key and the
#                  per-recipient webhook URLs, and the only one that ever writes
#                  .env. Keeping this in one place means there is exactly one path
#                  by which a secret enters the program, and one by which a
#                  generated one reaches disk.
# Assumptions: Secrets live in a `.env` file (see .env.example), never committed.
# Safety note: This module never logs or prints secret VALUES — only their names.
#              The bootstrap report carries names only, by construction.
# =============================================================================

from __future__ import annotations

import os
import re
import secrets as _secrets
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# A `.env` assignment line: optional leading whitespace, optional `export`, the NAME, `=`.
# The name is captured so the writer can match a key EXACTLY (`ORION_RELAY_TOKEN` must not
# match `ORION_RELAY_TOKEN_X`) and decide whether the value after `=` is empty.
_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

# The prefix every placeholder value in .env.example starts with. A copied example is the
# most likely way a "set" secret is not actually a secret, so the bootstrap flags it.
ENV_EXAMPLE_PLACEHOLDER_PREFIX = "replace-with-"


class SecretsError(Exception):
    """Raised when a required secret is missing from the environment.

    Why:
        Like ConfigError, this lets the CLI distinguish a *setup* problem (you
        forgot to fill in .env) from a real bug, and print a fixable message.
    """


def load_secrets(config_path: Path | None = None) -> None:
    """Load variables from .env file(s) into the process environment.

    Args:
        config_path: Path to the config file (orion.toml), when known. Its
            sibling `.env` (`config_path.parent / ".env"`) is loaded first. Pass
            None to rely only on the working-directory search.

    Returns:
        None. Side effect: populates os.environ with any keys found.

    Why:
        Secrets live next to the config, as Orion's *central* `.env` — but they
        are discovered by working directory by default, and a git hook or a
        scheduled job starts in some *other* directory (the tracked repo, or
        wherever the scheduler runs). So when we know the config path we load the
        `.env` beside it FIRST, which makes those non-interactive runs find the
        central secrets regardless of where they were invoked from. We then fall
        back to python-dotenv's default upward-from-CWD search, which covers
        running Orion from its own directory.

        Precedence: override=False (python-dotenv's default) means neither `.env`
        ever overwrites a value already present in the real environment (so an
        exported key still wins — useful in CI), and, because it is loaded first,
        the config-relative `.env` wins over a CWD one for any key both define.
        Loading a missing path is a harmless no-op, so passing a config whose
        directory has no `.env` is safe.
    """
    if config_path is not None:
        load_dotenv(dotenv_path=config_path.parent / ".env")
    load_dotenv()


def get_required(env_var: str) -> str:
    """Fetch a required secret by environment-variable name.

    Args:
        env_var: The name of the environment variable to read.

    Returns:
        The secret's value as a string (stripped of surrounding whitespace).

    Why:
        A missing webhook URL or API key should fail with a message naming the
        exact variable to set, not a downstream 401 or a confusing NoneType
        error. We treat empty/whitespace as missing because a blank line in .env
        is almost always a forgotten value, not an intentional empty secret.
    """
    value = os.environ.get(env_var)
    if value is None or not value.strip():
        raise SecretsError(
            f"Required secret {env_var!r} is not set. "
            f"Add it to your .env file (see .env.example)."
        )
    return value.strip()


@dataclass(frozen=True)
class BootstrapReport:
    """What `bootstrap_env_secrets` did, by variable NAME — never a value.

    Args:
        generated: Names that were absent and got a fresh value appended.
        filled: Names that were present with an EMPTY value and got a value in place.
        already_set: Names that were present with a non-empty value and were left alone.
        placeholders: The subset of `already_set` whose value looks like a copied
            .env.example placeholder (starts with ENV_EXAMPLE_PLACEHOLDER_PREFIX).

    Why:
        Four distinct outcomes, kept distinct: "generated", "filled (was empty)" and
        "already set" each mean something different to the operator, and a placeholder
        is the one "set" value that is not actually a secret. Collapsing them into
        set/unset would hide exactly the state that bites on a fresh deploy.
    """

    generated: tuple[str, ...] = ()
    filled: tuple[str, ...] = ()
    already_set: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()


def _generate_secret() -> str:
    """Return one fresh, high-entropy secret value.

    Returns:
        A 43-character URL-safe token (32 random bytes), the same recipe the docs give.

    Why:
        One place decides the strength and alphabet of every generated relay secret, so
        a change (longer, hex) is a one-line edit and every caller stays in step.
    """
    return _secrets.token_urlsafe(32)


def bootstrap_env_secrets(env_path: Path, names: Sequence[str]) -> BootstrapReport:
    """Generate any of `names` missing from the .env at `env_path`, writing safely (CS-O PR9).

    Args:
        env_path: The .env file to update (created if absent; its directory must exist).
        names: The variable names that must end up set. Order is preserved for appends.

    Returns:
        A BootstrapReport naming what was generated, filled, or left alone.

    Why:
        A relay host needs several independent random secrets before it can even start
        (the pepper, the session key, the admin token, sometimes the view token), and
        "run this python one-liner four times and paste carefully" is where fresh deploys
        go wrong. This does that job with the safety a secrets file deserves:
          - a NON-EMPTY value is NEVER overwritten — the operator's secret always wins,
            even when it looks wrong (a .env.example placeholder is reported, not replaced);
          - an EMPTY assignment (`NAME=`) is filled IN PLACE, because an empty line is
            almost always a forgotten value (the same reading get_required applies);
          - every other byte of the file is preserved — comments, ordering, blank lines,
            `export` prefixes and the file's newline style (CRLF stays CRLF);
          - the write is atomic (a temp file in the same directory, then os.replace), so a
            crash mid-write can never leave a truncated .env; and on POSIX the temp file is
            chmod 0600 BEFORE the rename, so the secrets are never readable by others for
            even an instant (Windows has no such bits — best-effort, documented).
        Values never enter the report or any message: names only.
    """
    existing = env_path.read_bytes().decode("utf-8") if env_path.exists() else ""
    newline = "\r\n" if "\r\n" in existing else "\n"
    # Split keeping NO terminators; we re-join with the detected newline so the style is
    # uniform on write. splitlines() also handles a file whose last line lacks a newline.
    lines = existing.splitlines() if existing else []

    # First pass: find which requested names already have an assignment, and whether it
    # carries a value. Only the LAST assignment of a name counts (python-dotenv's reading),
    # so the dict is overwritten as we scan.
    assignment_at: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = _ENV_ASSIGNMENT.match(line)
        if match and match.group(1) in names:
            assignment_at[match.group(1)] = index

    generated: list[str] = []
    filled: list[str] = []
    already_set: list[str] = []
    placeholders: list[str] = []
    for name in names:
        index = assignment_at.get(name)
        if index is None:
            lines.append(f"{name}={_generate_secret()}")
            generated.append(name)
            continue
        raw_value = _ENV_ASSIGNMENT.match(lines[index]).group(2).strip()
        # `NAME=` and `NAME=""` / `NAME=''` are the "forgot to fill it in" shapes.
        if raw_value in ("", '""', "''"):
            lines[index] = f"{name}={_generate_secret()}"
            filled.append(name)
            continue
        already_set.append(name)
        if raw_value.strip("\"'").startswith(ENV_EXAMPLE_PLACEHOLDER_PREFIX):
            placeholders.append(name)

    if generated or filled:
        content = newline.join(lines) + newline
        _write_private_atomic(env_path, content.encode("utf-8"))

    return BootstrapReport(
        generated=tuple(generated),
        filled=tuple(filled),
        already_set=tuple(already_set),
        placeholders=tuple(placeholders),
    )


def _write_private_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically with owner-only permissions where supported.

    Args:
        path: The destination file (its directory must exist).
        data: The full new contents.

    Why:
        A secrets file must never be observable half-written or world-readable. The temp
        file lives in the SAME directory so os.replace is a same-filesystem rename (atomic on
        POSIX and Windows); tempfile.mkstemp already creates it 0600 on POSIX, and the
        explicit chmod makes that intent visible rather than relying on a default. On
        Windows the mode bits are a no-op — the file inherits the directory ACL, which is
        the best the platform offers without a dependency (documented as best-effort).
    """
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.chmod(fd, 0o600)  # owner read/write only; no-op on Windows
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)  # never leave a stray temp file holding secrets
        raise
