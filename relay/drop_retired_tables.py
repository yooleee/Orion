# =============================================================================
# relay/drop_retired_tables.py
# -----------------------------------------------------------------------------
# Responsible for: The one-time, idempotent drop of the two RETIRED skills-comb
#                  tables (`relay_project_skills`, `relay_producer_skills`) from
#                  the relay's SQLite store, after the skills feature was removed
#                  at parity (living-resume retirement, Units 1-3). It takes a
#                  WAL-safe backup of the whole DB BEFORE dropping anything.
# Role in project: A standalone maintenance tool, run by hand against the relay's
#                  store as TWO deliberate invocations (modelled on
#                  relay.migrate_comments):
#                      python -m relay.drop_retired_tables --db PATH            # dry-run (default)
#                      python -m relay.drop_retired_tables --db PATH --drop     # back up, then drop
#                  Run from the repo root (repo root is on sys.path for `-m`); in
#                  production inside the container (PYTHONPATH=/app,
#                  --db /data/orion-relay.sqlite3). It is NOT part of the request
#                  path — the relay never imports it.
# Assumptions: Unit 3 removed both skills tables from the store's _SCHEMA, so
#              `open_relay_store` no longer recreates them — a fresh DB has neither,
#              and after this drop the live DB keeps them dropped. The tables are
#              rebuildable projections (nothing here is source-of-truth), so there is
#              nothing to migrate; the pre-drop backup exists purely as an operator
#              safety net, not because any data is irreplaceable.
# Safety: the set of tables this tool may drop is a LITERAL allowlist of exactly two
#         names. The tool NEVER accepts a table name as input, so it structurally
#         cannot touch `relay_project_meta` or the disciplines tables that sit
#         adjacent in the schema. A sentinel-table check also refuses a --db that is
#         not a relay store, so a mistyped path fails loudly instead of silently.
# =============================================================================

import argparse
import sqlite3
from pathlib import Path

# The ONLY tables this tool may ever drop. A LITERAL allowlist (living-resume
# retirement, the skills comb): the aggregate per-project set and the per-producer
# sibling. The tool never takes a table name from input, so `relay_project_meta` and
# both `*_disciplines` tables — which sit right beside these in the schema — cannot be
# reached, no matter how the tool is invoked.
_RETIRED_TABLES = ("relay_project_skills", "relay_producer_skills")

# A table every real relay store has. Its presence is a cheap sanity check that --db
# points at an actual relay DB and not an unrelated sqlite file (a mistyped path), so
# the tool refuses rather than operating on the wrong database.
_SENTINEL_TABLE = "relay_reports"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Return True if a table `name` exists in this database.

    Args:
        conn: An open sqlite connection to the relay store.
        name: The exact table name to look for.

    Returns:
        True when a table with that name is present, False otherwise.

    Why:
        Both the dry-run report and the drop path need "does this table exist?" — the
        drop uses IF EXISTS for idempotency, but we still check first so we can report
        accurate before/after counts and treat an absent table as a clean no-op.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _row_count(conn: sqlite3.Connection, name: str) -> int:
    """Return the number of rows in table `name`.

    Args:
        conn: An open sqlite connection.
        name: A table name — MUST be one of `_RETIRED_TABLES`.

    Returns:
        The row count.

    Why:
        The operator wants to see how many rows are about to be dropped (a dry-run
        spot-check). The table name is interpolated into the SQL because sqlite cannot
        parameterize an identifier; that is safe ONLY because the name is a literal from
        our own allowlist, never user input — the assert makes that invariant explicit.
    """
    assert name in _RETIRED_TABLES, f"refusing to count non-allowlisted table {name!r}"
    return conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]


def _existing_retired_tables(conn: sqlite3.Connection) -> list[str]:
    """Return the allowlisted tables that are actually present, in allowlist order.

    Args:
        conn: An open sqlite connection.

    Returns:
        The subset of `_RETIRED_TABLES` that exists in this DB (possibly empty).

    Why:
        Drives both the report and the "nothing to do" no-op: on a fresh DB or a second
        run this is empty, so the tool skips the backup and drop entirely.
    """
    return [t for t in _RETIRED_TABLES if _table_exists(conn, t)]


def _assert_relay_db(conn: sqlite3.Connection) -> None:
    """Raise if the connected DB does not look like a relay store.

    Args:
        conn: An open sqlite connection to the candidate DB.

    Raises:
        SystemExit: with a clear message when the sentinel table is absent.

    Why:
        A drop is destructive, so pointing --db at the wrong file must fail loudly, not
        quietly do nothing (or worse, on some other schema). Requiring the relay's own
        sentinel table present is a cheap guard against a mistyped path.
    """
    if not _table_exists(conn, _SENTINEL_TABLE):
        raise SystemExit(
            f"error: {_SENTINEL_TABLE!r} not found — --db does not look like a relay "
            f"store. Refusing to operate on it."
        )


def _backup_db(conn: sqlite3.Connection, backup_path: Path) -> None:
    """Write a consistent, WAL-safe copy of the whole DB to `backup_path`.

    Args:
        conn: The open (source) connection to the live relay store.
        backup_path: Where to write the backup. Must NOT already exist.

    Raises:
        SystemExit: if `backup_path` already exists (never clobber a prior safety copy).

    Why:
        `sqlite3.Connection.backup()` uses SQLite's online backup API, which produces a
        transactionally-consistent single-file copy even when the source is in WAL mode
        (a plain `cp` of a WAL database can miss committed pages still in the -wal file).
        We refuse to overwrite an existing backup so an accidental re-invocation can
        never destroy the copy taken on the first run.
    """
    if backup_path.exists():
        raise SystemExit(
            f"error: backup path {backup_path} already exists — refusing to overwrite "
            f"an existing safety copy. Move or remove it, or pass a different --backup."
        )
    dest = sqlite3.connect(backup_path)
    try:
        conn.backup(dest)  # online backup: consistent even under WAL
    finally:
        dest.close()


def _default_backup_path(db_path: Path) -> Path:
    """Derive a default backup path next to the DB.

    Args:
        db_path: The relay store path passed via --db.

    Returns:
        `<db>.before-skills-drop.bak` beside the original file.

    Why:
        A predictable, self-describing name placed next to the DB so the operator can
        find (and later delete) it without guessing. The name records exactly what the
        copy predates.
    """
    return db_path.with_name(db_path.name + ".before-skills-drop.bak")


def cmd_run(conn: sqlite3.Connection, *, drop: bool, backup_path: Path) -> int:
    """Report (and, with drop=True, back up then drop) the retired skills tables.

    Args:
        conn: An open connection to the relay store (already sentinel-checked).
        drop: When False (the default), report existence + row counts and change
            nothing. When True, take a full backup and then drop the present
            allowlisted tables.
        backup_path: Where the pre-drop backup is written (only used when drop=True and
            at least one table is present).

    Returns:
        0 in every normal case (a clean dry-run, a real drop, or a no-op re-run).

    Why:
        The single decision point of the tool. Dry-run is the DEFAULT so the destructive
        action is never the accident — the operator must add --drop deliberately, having
        first seen the dry-run counts. The backup happens BEFORE the first DROP so the
        safety copy predates any change, and only when there is actually something to
        drop (a re-run with nothing present skips backup and drop alike).
    """
    present = _existing_retired_tables(conn)

    # Report the current state of BOTH allowlisted tables (present or already gone).
    mode = "drop" if drop else "dry-run"
    print(f"drop_retired_tables ({mode}): skills-comb retirement")
    for name in _RETIRED_TABLES:
        if _table_exists(conn, name):
            print(f"  present: {name} ({_row_count(conn, name)} row(s))")
        else:
            print(f"  absent:  {name} (already dropped)")

    if not present:
        print("  nothing to drop — all retired tables are already absent.")
        return 0

    if not drop:
        names = ", ".join(present)
        print(f"  --dry-run: would back up the DB, then drop {len(present)} table(s): {names}")
        print("  re-run with --drop to apply.")
        return 0

    # --drop: safety copy FIRST (predates any change), then drop only the present
    # allowlisted tables. DROP TABLE IF EXISTS keeps this idempotent; the assert is a
    # belt-and-braces restatement that we only ever drop allowlisted names.
    print(f"  backing up to {backup_path} ...")
    _backup_db(conn, backup_path)
    print("  backup complete.")
    for name in present:
        assert name in _RETIRED_TABLES, f"refusing to drop non-allowlisted table {name!r}"
        conn.execute(f"DROP TABLE IF EXISTS {name}")
        print(f"  dropped: {name}")
    conn.commit()
    print(f"  done — dropped {len(present)} table(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser (--db required; --drop opt-in; --backup optional).

    Returns:
        A configured ArgumentParser.

    Why:
        Mirrors relay.migrate_comments' CLI style. There is no table-name argument by
        design — the allowlist is hardcoded — so the only choices are which DB, whether
        to actually drop, and where to put the backup. Dry-run is the default (no flag),
        making the destructive path the deliberate one.
    """
    parser = argparse.ArgumentParser(
        prog="python -m relay.drop_retired_tables",
        description="One-time, idempotent, backup-first drop of the retired skills-comb "
        "tables (relay_project_skills, relay_producer_skills).",
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to the relay's sqlite store (e.g. /data/orion-relay.sqlite3).",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="Actually drop the tables (after a backup). Without this, only report "
        "(dry-run).",
    )
    parser.add_argument(
        "--backup",
        default=None,
        help="Where to write the pre-drop backup (default: <db>.before-skills-drop.bak). "
        "Must not already exist. Only used with --drop.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, open the store, sanity-check it, and dispatch.

    Args:
        argv: Argument vector (defaults to sys.argv[1:]). Passed explicitly by tests.

    Returns:
        The subcommand's exit code (0 in normal cases).

    Why:
        A thin entry point that owns argument parsing and the DB connection. It uses a
        plain sqlite3 connection (NOT open_relay_store) deliberately: this tool must not
        recreate any schema as a side effect — it only inspects and drops.
    """
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    backup_path = Path(args.backup) if args.backup else _default_backup_path(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _assert_relay_db(conn)
        return cmd_run(conn, drop=args.drop, backup_path=backup_path)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
