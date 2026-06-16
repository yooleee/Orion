# =============================================================================
# secrets.py
# -----------------------------------------------------------------------------
# Responsible for: Loading secrets from a gitignored .env into the environment
#                  and handing them out by name, with a clear error when missing.
# Role in project: The only module that reads the Anthropic API key and the
#                  per-recipient webhook URLs. Keeping this in one place means
#                  there is exactly one path by which a secret enters the program.
# Assumptions: Secrets live in a `.env` file (see .env.example), never committed.
# Safety note: This module never logs or prints secret VALUES — only their names.
# =============================================================================

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


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
