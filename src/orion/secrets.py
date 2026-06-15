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


def load_secrets(dotenv_path: Path | None = None) -> None:
    """Load variables from a .env file into the process environment.

    Args:
        dotenv_path: Optional explicit path to a .env file. If None,
            python-dotenv searches upward from the current directory.

    Returns:
        None. Side effect: populates os.environ with any keys found.

    Why:
        Centralizing the load means callers just ask for secrets by name after
        this runs. We pass override=False (python-dotenv's default) so a value
        already set in the real environment wins over the .env file — useful in
        CI or when a user exports a key directly.
    """
    if dotenv_path is not None:
        load_dotenv(dotenv_path=dotenv_path)
    else:
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
