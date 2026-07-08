# =============================================================================
# relay/migrate_comments.py
# -----------------------------------------------------------------------------
# Responsible for: The one-time, idempotent migration that folds legacy C2
#                  `report_comments` rows into the E2 Inc 5 discussion model
#                  (`relay_discussion_items`), then drops the comment table.
#                  This is the parity step of KI-28 Stage 2 (comments retired
#                  outright) — see docs/stage2-comments-discussion-consolidation-
#                  kickoff.md, the 2026-07-07 addendum.
# Role in project: A standalone maintenance tool, run by hand against the relay's
#                  SQLite store as TWO deliberate invocations:
#                      python -m relay.migrate_comments migrate --db PATH [--dry-run]
#                      python -m relay.migrate_comments drop    --db PATH [--dry-run]
#                  Run from the repo root (repo root is on sys.path for `-m`); in
#                  production inside the container (PYTHONPATH=/app,
#                  --db /data/orion-relay.sqlite3). It is NOT part of the request
#                  path — the relay never imports it.
# Assumptions: `open_relay_store` (re)creates the schema on connect via
#              `IF NOT EXISTS`, so `relay_discussion_items` is guaranteed present.
#              `report_comments.report_id` points at `relay_reports.id` but is NOT
#              an enforced foreign key, so an "orphan" comment (a report_id with no
#              report row) is possible and is handled explicitly rather than lost.
# =============================================================================

import argparse
import sqlite3
import sys
from pathlib import Path

from relay.store import add_discussion_item, open_relay_store

# The historical role we assign migrated comments. Legacy C2 comments carried no
# identity (free-text author, role NULL); they were supervisor feedback in intent,
# so "supervisor" is recorded honestly as a best-effort historical mapping. This is
# the ONE place identity is assigned rather than server-derived (the migration is
# the documented exception to the attribution invariant).
_MIGRATED_ROLE = "supervisor"

# Display name used when the legacy `author` was blank (comments allowed an empty
# author pre-C3). Chosen over "" so the thread renders a real label, not a gap.
_ANONYMOUS_AUTHOR = "anonymous"

# How many mapped rows a --dry-run prints as a spot-check sample.
_DRY_RUN_SAMPLE = 5


def _migrated_body(report_id: int, body: str) -> str:
    """Prefix a comment's body with the report it referred to.

    Args:
        report_id: The relay_reports.id the comment hung off (its only real context).
        body: The original comment text, preserved verbatim.

    Returns:
        The body with a leading `[re: report {report_id}]` line, the original words
        on the lines below it.

    Why:
        Stage 2 retires the report anchor outright — discussion items anchor on the
        project, not a report — so the report context would otherwise be lost. Folding
        it into the body as a header line keeps parity on INFORMATION while staying
        honest about the schema (the item genuinely has no report column). This exact
        string is also the migration's dedup key, so it must be deterministic.
    """
    return f"[re: report {report_id}]\n{body}"


def _report_comments_exists(conn: sqlite3.Connection) -> bool:
    """Return True if the legacy `report_comments` table is present in this DB.

    Args:
        conn: An open relay-store connection.

    Returns:
        True when the table exists, False otherwise.

    Why:
        The current relay schema no longer creates `report_comments` (KI-28 Stage 2
        removed it from _SCHEMA), so `open_relay_store` does NOT recreate it. This tool
        runs against a LEGACY DB where the table still holds data; but on a fresh DB — or
        after `drop` — the table is absent. Checking first lets migrate/drop treat "no
        table" as "nothing to do" (a clean no-op) instead of raising `no such table`.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='report_comments'"
    ).fetchone()
    return row is not None


def _resolved_comments(conn: sqlite3.Connection) -> list[dict]:
    """Return every comment joined to its project, oldest first.

    Args:
        conn: An open relay-store connection.

    Returns:
        A list of {"id", "report_id", "author", "body", "created_at", "project"} dicts,
        one per comment whose report_id resolves to a real report, in ascending-id
        (chronological) order. Empty when the legacy table is absent.

    Why:
        `report_comments` has no project column — the project lives on the report it
        hangs off — so we JOIN through relay_reports to resolve the anchor, exactly as
        the retired `comments_for_project` did. This INNER join silently excludes
        orphan comments (no matching report); those are surfaced separately by
        `_orphan_comments` so nothing is dropped without being reported.
    """
    if not _report_comments_exists(conn):
        return []
    rows = conn.execute(
        """
        SELECT c.id, c.report_id, c.author, c.body, c.created_at, r.project
        FROM report_comments c
        JOIN relay_reports r ON c.report_id = r.id
        ORDER BY c.id ASC
        """
    ).fetchall()
    return [
        {
            "id": row["id"],
            "report_id": row["report_id"],
            "author": row["author"],
            "body": row["body"],
            "created_at": row["created_at"],
            "project": row["project"],
        }
        for row in rows
    ]


def _orphan_comments(conn: sqlite3.Connection) -> list[dict]:
    """Return comments whose report_id has no matching report, oldest first.

    Args:
        conn: An open relay-store connection.

    Returns:
        A list of {"id", "report_id"} dicts for every comment the INNER join in
        `_resolved_comments` would exclude. Empty in the expected case (this system
        never deletes reports).

    Why:
        `report_id` is deliberately not an enforced foreign key, so an orphan is
        structurally possible. An orphan has no project to attach to, so it cannot be
        migrated. Rather than let the inner join drop it silently (violating "no comment
        is lost"), we detect orphans with a LEFT join and report them loudly — and the
        drop guard refuses while any exist, so a human decides what to do.
    """
    if not _report_comments_exists(conn):
        return []
    rows = conn.execute(
        """
        SELECT c.id, c.report_id
        FROM report_comments c
        LEFT JOIN relay_reports r ON c.report_id = r.id
        WHERE r.id IS NULL
        ORDER BY c.id ASC
        """
    ).fetchall()
    return [{"id": row["id"], "report_id": row["report_id"]} for row in rows]


def _matching_discussion_row_id(
    conn: sqlite3.Connection,
    project: str,
    author_name: str,
    created_at: str,
    body: str,
) -> int | None:
    """Return the id of a discussion row already matching this migrated comment, or None.

    Args:
        conn: An open relay-store connection.
        project: The thread anchor the comment maps to.
        author_name: The mapped display name ("anonymous" when the original was blank).
        created_at: The comment's preserved timestamp.
        body: The already-prefixed migrated body (`_migrated_body` output).

    Returns:
        The id of the (lowest-id) matching row, or None when no row matches.

    Why:
        This four-tuple `(project, author_name, created_at, migrated_body)` is the
        migration's idempotency key (per the addendum): a comment whose key already exists
        was already migrated, so a re-run skips it — that is what makes re-running safe.
        role/author_id are excluded from the key deliberately (the mapping fixes them).
        Returning the ROW ID (not just a bool) lets the drop guard enforce a **one-to-one**
        mapping: because the key is content-based and `created_at` is only second-precise,
        two genuinely-distinct comments can share a key and collapse onto one row — which,
        if the source table were then dropped, would LOSE the duplicate. The drop guard uses
        these ids to detect that collapse and refuse, upholding the "no comment is lost"
        invariant instead of silently accepting the loss.
    """
    row = conn.execute(
        """
        SELECT id FROM relay_discussion_items
        WHERE project = ? AND author_name = ? AND created_at = ? AND body = ?
        ORDER BY id ASC LIMIT 1
        """,
        (project, author_name, created_at, body),
    ).fetchone()
    return row["id"] if row is not None else None


def _map_comment(comment: dict) -> dict:
    """Map one resolved comment to its discussion-item fields.

    Args:
        comment: A dict from `_resolved_comments` (carries project + the raw comment).

    Returns:
        A dict of the discussion-item fields this comment becomes: project, author_id
        (always None), author_name, role, body (prefixed), created_at.

    Why:
        Centralizes the mapping rule in one place so migrate, dry-run, and the drop
        parity guard all derive the SAME target row (DRY) — the guard can only trust
        "already migrated?" if it computes the identical body and author_name the
        migrate step wrote.
    """
    author = comment["author"]
    return {
        "project": comment["project"],
        "author_id": None,  # legacy comments never carried an authenticated identity
        "author_name": author if author.strip() else _ANONYMOUS_AUTHOR,
        "role": _MIGRATED_ROLE,
        "body": _migrated_body(comment["report_id"], comment["body"]),
        "created_at": comment["created_at"],
    }


def cmd_migrate(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    """Migrate report_comments into relay_discussion_items (idempotent).

    Args:
        conn: An open relay-store connection (its schema is already ensured).
        dry_run: When True, compute and report everything but insert nothing.

    Returns:
        0 on success (including a clean re-run where everything is already migrated).

    Why:
        The parity step of Stage 2: every existing comment becomes a discussion item,
        preserving body/author/timestamp, so the comment system can be retired at
        parity. Idempotent by design (the four-tuple check) so it is safe to re-run —
        the required property for a migration that touches the live relay DB. Genuine
        duplicates (two distinct comments sharing the four-tuple) are NOT silently
        collapsed away: they are reported here, and the drop guard refuses while any
        exists, so no comment is ever lost on the destructive step.
    """
    resolved = _resolved_comments(conn)
    orphans = _orphan_comments(conn)
    total = len(resolved) + len(orphans)
    projects = sorted({c["project"] for c in resolved})

    migrated = 0
    already = 0
    duplicates: list[dict] = []
    # Four-tuple keys this run has migrated. Lets us tell a genuine duplicate (a distinct
    # source comment colliding with one THIS run just migrated) from an idempotent re-run
    # skip (a comment a PRIOR run migrated). Tracked even under --dry-run so the preview
    # surfaces duplicates too.
    migrated_keys: set[tuple[str, str, str, str]] = set()
    sample: list[dict] = []
    for comment in resolved:
        mapped = _map_comment(comment)
        key = (
            mapped["project"],
            mapped["author_name"],
            mapped["created_at"],
            mapped["body"],
        )
        if key in migrated_keys:
            # A distinct source comment with a four-tuple identical to one already migrated
            # THIS run — a real duplicate that would collapse onto the same row. Don't write
            # a second identical row (the key IS the identity); record it so the operator
            # sees it. The drop guard refuses while this collapse stands, so nothing is lost.
            duplicates.append(comment)
            continue
        if _matching_discussion_row_id(
            conn,
            mapped["project"],
            mapped["author_name"],
            mapped["created_at"],
            mapped["body"],
        ) is not None:
            already += 1
            continue
        if len(sample) < _DRY_RUN_SAMPLE:
            sample.append(mapped)
        if not dry_run:
            add_discussion_item(
                conn,
                mapped["project"],
                mapped["author_id"],
                mapped["author_name"],
                mapped["role"],
                mapped["body"],
                mapped["created_at"],
            )
        migrated_keys.add(key)
        migrated += 1

    prefix = "migrate --dry-run" if dry_run else "migrate"
    verb = "would migrate" if dry_run else "migrated"
    print(f"{prefix}: {total} comment(s) found across {len(projects)} project(s)")
    print(f"  {verb}:              {migrated}")
    print(f"  already migrated:    {already}")
    print(f"  duplicate/collapsed: {len(duplicates)}")
    print(f"  skipped (orphan):    {len(orphans)}")
    if duplicates:
        # Loud, itemized report — a duplicate shares its target row with another comment, so
        # dropping report_comments would lose it. The drop guard refuses until it is resolved.
        print(
            f"  WARNING: {len(duplicates)} comment(s) are duplicates (identical project, "
            f"author, second-precise timestamp, and body) that collapse onto an already-"
            f"migrated row — NOT separately migrated; `drop` will refuse until resolved:",
            file=sys.stderr,
        )
        for comment in duplicates:
            print(
                f"    comment id={comment['id']} in project {comment['project']!r} "
                f"(report_id={comment['report_id']})",
                file=sys.stderr,
            )
    if orphans:
        # Loud, itemized report — an orphan cannot be migrated (no project), so it must
        # never disappear quietly. The drop guard will also refuse while these exist.
        print(
            f"  WARNING: {len(orphans)} orphan comment(s) reference a missing report "
            f"and were NOT migrated:",
            file=sys.stderr,
        )
        for orphan in orphans:
            print(
                f"    comment id={orphan['id']} -> missing report_id={orphan['report_id']}",
                file=sys.stderr,
            )
    if dry_run and sample:
        print(f"  sample of the first {len(sample)} row(s) that would be written:")
        for mapped in sample:
            first_line = mapped["body"].splitlines()[0]
            print(
                f"    [{mapped['project']}] {mapped['author_name']} "
                f"({mapped['role']}) @ {mapped['created_at']}: {first_line}"
            )
    return 0


def cmd_drop(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    """Drop report_comments — but only once every comment is migrated (parity guard).

    Args:
        conn: An open relay-store connection.
        dry_run: When True, report the parity result and row count but drop nothing.

    Returns:
        0 when the table is dropped (or already gone, or a clean dry-run); 1 when the
        parity guard refuses because a comment is unmigrated or orphaned.

    Why:
        The deliberate second invocation of migrate-and-drop. The guard re-derives each
        comment's mapped row and enforces a **one-to-one** mapping BEFORE dropping —
        every comment must have a matching discussion row (nothing unmigrated), no two
        comments may share the same row (no collapsed duplicate), and there must be no
        orphans. Any of those means dropping would lose a comment, so it refuses. This
        makes "no comment is lost" a structural guarantee, not a trusted claim.
        `DROP TABLE IF EXISTS` makes a second drop a clean no-op.
    """
    resolved = _resolved_comments(conn)
    orphans = _orphan_comments(conn)

    unmatched = []
    # Discussion row id -> the source comments that map onto it. A row with >1 comment is a
    # collapse: those comments are indistinguishable under the content key, so dropping the
    # source table would keep one and lose the rest.
    comments_by_row: dict[int, list[dict]] = {}
    for comment in resolved:
        mapped = _map_comment(comment)
        row_id = _matching_discussion_row_id(
            conn,
            mapped["project"],
            mapped["author_name"],
            mapped["created_at"],
            mapped["body"],
        )
        if row_id is None:
            unmatched.append(comment)
        else:
            comments_by_row.setdefault(row_id, []).append(comment)

    collapsed = {rid: cs for rid, cs in comments_by_row.items() if len(cs) > 1}

    if unmatched or orphans or collapsed:
        print(
            "drop refused: dropping now would lose a comment — resolve the items below and "
            "re-run `migrate` first.",
            file=sys.stderr,
        )
        for comment in unmatched:
            print(
                f"  unmigrated: comment id={comment['id']} in project "
                f"{comment['project']!r}",
                file=sys.stderr,
            )
        for row_id, cs in collapsed.items():
            ids = ", ".join(str(c["id"]) for c in cs)
            print(
                f"  collapsed: comment ids [{ids}] all map to discussion row {row_id} "
                f"(identical project/author/timestamp/body) — dropping would lose "
                f"{len(cs) - 1} of them",
                file=sys.stderr,
            )
        for orphan in orphans:
            print(
                f"  orphan: comment id={orphan['id']} -> missing "
                f"report_id={orphan['report_id']}",
                file=sys.stderr,
            )
        return 1

    count = (
        conn.execute("SELECT COUNT(*) AS n FROM report_comments").fetchone()["n"]
        if _report_comments_exists(conn)
        else 0
    )
    if dry_run:
        print(f"drop --dry-run: parity OK; would drop report_comments ({count} row(s))")
        return 0

    # Dropping the table drops its index (idx_report_comments_report) with it. IF EXISTS
    # keeps a re-run a clean no-op. The table stays dropped: Unit 0.3a removed it from the
    # store's _SCHEMA, so open_relay_store no longer recreates it on the next server start.
    conn.execute("DROP TABLE IF EXISTS report_comments")
    conn.commit()
    print(f"drop: report_comments dropped ({count} row(s) removed)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with the `migrate` and `drop` subcommands.

    Returns:
        A configured ArgumentParser. `--db` is required on each subcommand; `--dry-run`
        is an opt-in preview.

    Why:
        Mirrors the repo's CLI style (argparse subparsers, per-command options) so the
        tool reads like the rest of Orion. Two subcommands make the migrate-and-drop
        "two deliberate invocations" explicit at the command line, not a hidden flag.
    """
    parser = argparse.ArgumentParser(
        prog="python -m relay.migrate_comments",
        description="One-time, idempotent migration of report_comments into the "
        "discussion model (KI-28 Stage 2).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("migrate", "Fold report_comments into relay_discussion_items (idempotent)."),
        ("drop", "Drop report_comments once every comment is migrated (parity-guarded)."),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--db",
            required=True,
            help="Path to the relay's sqlite store (e.g. /data/orion-relay.sqlite3).",
        )
        sub.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen without writing anything.",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, open the store, and dispatch to the chosen subcommand.

    Args:
        argv: Argument vector (defaults to sys.argv[1:]). Passed explicitly by tests.

    Returns:
        The subcommand's exit code (0 success, 1 on a refused drop).

    Why:
        A thin entry point: it owns argument parsing and the DB connection (opened via
        the same `open_relay_store` the server uses, so the schema is ensured), and
        delegates the actual work to the testable cmd_* functions.
    """
    args = build_parser().parse_args(argv)
    conn = open_relay_store(Path(args.db))
    try:
        if args.command == "migrate":
            return cmd_migrate(conn, dry_run=args.dry_run)
        return cmd_drop(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
