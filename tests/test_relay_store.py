# =============================================================================
# tests/test_relay_store.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the relay's own SQLite store — idempotent open,
#                  ingest, and the read queries (list/history/get) the dashboard
#                  renders from.
# Role in project: The relay store is the hosted half's persistence and is
#                  deliberately independent of orion.state. These tests pin that a
#                  pushed blob round-trips faithfully (sections/participants as
#                  structures, not strings), that history/list order correctly, and
#                  that a missing id is a clean None (→ a 404 later), so CP6/CP7 can
#                  build on a trustworthy store.
# Test approach: blob dicts are hand-built to the portable contract's shape (NOT
#                via orion.report) so the store test mirrors the store's own
#                independence from orion — CP6 proves the real end-to-end contract.
# =============================================================================

import sqlite3
import threading
from datetime import date

import pytest

from relay import store
from relay.store import (
    add_credential,
    add_discussion_item,
    add_user,
    bump_session_version,
    delete_user,
    discussion_items_for_project,
    effective_checklist,
    get,
    get_active_password_credential,
    get_checklist,
    get_credential_by_key_verifier,
    get_due_soon_days,
    get_project_kind,
    get_user_by_id,
    get_user_by_name,
    history,
    ingest,
    latest_report_per_project,
    list_credentials,
    list_projects,
    list_users,
    observed_history,
    open_relay_store,
    producer_checklists_for,
    producer_disciplines_for,
    project_disciplines,
    RelayStoreMigrationError,
    projects_for_user,
    record_admin_audit,
    record_observations,
    revoke_credential,
    rename_user,
    revoke_user,
    set_due_soon_days,
    set_project_kind,
    update_last_login,
    upsert_checklist,
    upsert_producer_checklist,
    upsert_disciplines,
    upsert_producer_disciplines,
)


def _blob(project="demo", *, generated_at="2026-06-18T00:00:00+00:00", sections=None):
    """Build a portable-blob dict in the shape serialize_blob produces.

    Args:
        project: The project name for the blob.
        generated_at: The blob's build timestamp (varied to test ordering).
        sections: The (title, body) sections as a list of 2-element lists; defaults
            to one section when None.

    Why:
        Centralizes blob construction so each test varies only the field it checks
        (DRY). Built as a plain dict — not through orion.report — to keep this test
        independent of the local package, exactly as the store itself is.
    """
    if sections is None:
        sections = [["Code activity", "Shipped the seam."]]
    return {
        "project": project,
        "participants": ["Alex", "Sam"],
        "share_level": "high_level",
        "lane": "raw",
        "body": "Shipped the seam.",
        "generated_at": generated_at,
        "orion_version": "0.0.0",
        "sections": sections,
    }


def test_open_is_idempotent(tmp_path):
    """Opening the same store twice succeeds and preserves data.

    Why this matters: open_relay_store runs the schema on every open (IF NOT
    EXISTS). Re-opening must not error or wipe existing rows — the server may open
    the store on every start, so a second open has to be a safe no-op over real
    data.
    """
    db = tmp_path / "relay.sqlite3"
    conn = open_relay_store(db)
    new_id = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")
    conn.close()

    # Re-open the same file: schema re-applied, and the earlier row still there.
    conn2 = open_relay_store(db)
    assert get(conn2, new_id) is not None


def test_ingest_then_get_round_trips_all_fields(tmp_path):
    """A blob ingested then fetched by id returns every field, decoded.

    Why this matters: this is the store's core promise — what the dashboard renders
    must equal what was pushed. We especially pin that sections and participants
    come back as STRUCTURES (lists), not the JSON strings they are stored as, so
    the renderer gets real (title, body) pairs and names.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    blob = _blob(sections=[["Code activity", "Did X."], ["Notes", "Thinking Y."]])

    new_id = ingest(conn, blob, "2026-06-18T01:02:03+00:00")
    report = get(conn, new_id)

    assert report["id"] == new_id
    assert report["project"] == "demo"
    assert report["body"] == "Shipped the seam."
    assert report["share_level"] == "high_level"
    assert report["lane"] == "raw"
    assert report["generated_at"] == "2026-06-18T00:00:00+00:00"
    assert report["orion_version"] == "0.0.0"
    assert report["ingested_at"] == "2026-06-18T01:02:03+00:00"
    # The JSON columns decode back to structures, not strings.
    assert report["participants"] == ["Alex", "Sam"]
    assert report["sections"] == [["Code activity", "Did X."], ["Notes", "Thinking Y."]]


def test_ingest_records_report_author_and_defaults_to_none(tmp_path):
    """A push records its producer's id + name; an unattributed push stores NULL both.

    Why this matters: C3 Inc 2 report attribution. The identified producer's id and (snapshotted)
    name must round-trip through get(); a legacy/anonymous push leaves both NULL, which the
    dashboard renders as no "pushed by" — the same shape old, pre-attribution rows have.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    attributed = ingest(
        conn, _blob("demo"), "2026-06-18T01:00:00+00:00", author_id=7, author_name="Teammate B"
    )
    legacy = ingest(conn, _blob("demo"), "2026-06-18T02:00:00+00:00")  # no author

    a = get(conn, attributed)
    assert a["author_id"] == 7 and a["author_name"] == "Teammate B"
    l = get(conn, legacy)
    assert l["author_id"] is None and l["author_name"] is None


def test_record_observations_stamps_the_observing_author(tmp_path):
    """Each observation row carries the pushing producer's id (NULL on a legacy push).

    Why this matters: observation provenance is captured at write time because it cannot be
    reconstructed later — a future per-producer slippage view will read author_id. We assert an
    attributed push stamps it and a legacy push leaves it NULL. (author_id is not yet surfaced on
    any read path, so we query the row directly.)
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(
        conn, "demo", [{"text": "a", "done": False}], "2026-06-18T00:00:00+00:00", author_id=7
    )
    record_observations(conn, "demo", [{"text": "b", "done": True}], "2026-06-18T00:01:00+00:00")

    rows = conn.execute(
        "SELECT item_key, author_id FROM relay_observed_items ORDER BY id"
    ).fetchall()
    assert (rows[0]["item_key"], rows[0]["author_id"]) == ("a", 7)
    assert rows[1]["item_key"] == "b" and rows[1]["author_id"] is None


def test_ensure_columns_migrates_a_pre_attribution_schema(tmp_path):
    """Reopening a DB whose relay_reports predates the author columns adds them; old rows read NULL.

    Why this matters: this is the store's FIRST real migration — an already-deployed relay created
    relay_reports without author columns, and `CREATE TABLE IF NOT EXISTS` will never add them.
    `_ensure_columns` must ALTER them in on open, and the existing row must read NULL (it predates
    attribution). We build the old-shape table by hand, then let open_relay_store migrate it.
    """
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE relay_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, body TEXT NOT NULL,
            sections TEXT NOT NULL, participants TEXT NOT NULL, share_level TEXT NOT NULL,
            lane TEXT NOT NULL, generated_at TEXT NOT NULL, orion_version TEXT NOT NULL,
            ingested_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO relay_reports "
        "(project, body, sections, participants, share_level, lane, generated_at, "
        " orion_version, ingested_at) "
        "VALUES ('demo','b','[]','[]','high_level','raw',"
        "'2026-06-18T00:00:00+00:00','0.0.0','2026-06-18T00:00:01+00:00')"
    )
    conn.commit()
    conn.close()

    conn = open_relay_store(db)  # runs _ensure_columns → the ALTERs land
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(relay_reports)")}
    assert {"author_id", "author_name"} <= cols
    migrated = get(conn, 1)  # the pre-attribution row
    assert migrated["author_id"] is None and migrated["author_name"] is None
    conn.close()

    # Idempotent: a second open must not error (guarded ADD COLUMN is a no-op once present).
    open_relay_store(db).close()


def test_ensure_columns_survives_two_concurrent_opens(tmp_path):
    """Two threads migrating the same un-migrated DB at once must both succeed.

    Why this matters: a pre-existing race, found while building the Unit 2a migration. The
    `PRAGMA table_info` guard is check-then-act, so two workers opening a not-yet-migrated DB
    simultaneously both see the column missing and both ALTER — the loser raised
    OperationalError("duplicate column name"). Because the relay opens the store on EVERY
    request, that surfaced as a 500 on a real request during precisely the redeploy a migration
    runs on. The fix treats an already-present column as success.
    """
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")  # as a deployed relay's DB already is — see _seed_pre_revamp_db
    conn.execute(
        "CREATE TABLE relay_observed_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "project TEXT NOT NULL, item_key TEXT NOT NULL, text TEXT NOT NULL, "
        "done INTEGER NOT NULL, observed_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    errors = []
    barrier = threading.Barrier(2)

    def open_it():
        try:
            barrier.wait()  # force both threads into the ALTER at the same moment
            open_relay_store(db).close()
        except Exception as exc:  # noqa: BLE001 — surface ANY failure, that is the point
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    conn = open_relay_store(db)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(relay_observed_items)")}
    assert "author_id" in cols  # the column landed exactly once, from whichever thread won
    conn.close()


# --- Auth revamp Unit 2a: the credential backfill (the store's first DATA migration) ---
# Every prior migration was DDL. This one copies data, runs on a per-request open, and must
# survive concurrency and interruption — so the cases below are about those properties, not
# just "the rows appeared".

# A faithful copy of the pre-revamp relay_users schema, seeded via raw SQL so the test builds
# a genuinely OLD database rather than a new one with rows in it.
_PRE_REVAMP_USERS_DDL = """
CREATE TABLE relay_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    key_verifier    TEXT NOT NULL UNIQUE,
    role            TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);
"""


def _seed_pre_revamp_db(db, users):
    """Build an old-schema DB holding `users` as (name, verifier, role, active) tuples.

    The DB is put in WAL mode here because that is what a real pre-revamp relay DB is: it was
    created by `open_relay_store`, which sets WAL on open. Seeding it in the default
    rollback-journal mode would make the concurrency tests below exercise SQLite's one-time
    WAL CONVERSION race (that conversion takes a brief exclusive lock and does not honor the
    busy timeout) instead of the migration race they are actually about.
    """
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_PRE_REVAMP_USERS_DDL)
    for name, verifier, role, active in users:
        conn.execute(
            "INSERT INTO relay_users (name, key_verifier, role, active, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 'test', '2026-07-19T00:00:00+00:00')",
            (name, verifier, role, active),
        )
    conn.commit()
    conn.close()


def test_open_backfills_a_key_credential_for_every_pre_revamp_account(tmp_path):
    """Opening a pre-revamp DB gives every existing account a key credential with the SAME verifier.

    Why this matters: this is the promise that makes the revamp deployable — "existing keys keep
    working with zero re-provisioning". The verifier must be COPIED, not re-minted, because the raw
    key it verifies lives on the user's machines and cannot be regenerated from here. If this copy
    were wrong, every deployed key would break at the Unit 2b cutover with no way to recover them.
    """
    db = tmp_path / "old.sqlite3"
    _seed_pre_revamp_db(db, [("user", "verifier-a", "admin", 1), ("dad", "verifier-b", "viewer", 1)])

    conn = open_relay_store(db)
    creds = conn.execute(
        "SELECT u.name, c.type, c.label, c.verifier, c.active FROM relay_credentials c "
        "JOIN relay_users u ON u.id = c.user_id ORDER BY u.name"
    ).fetchall()
    assert [(r["name"], r["type"], r["verifier"]) for r in creds] == [
        ("dad", "key", "verifier-b"),
        ("user", "key", "verifier-a"),
    ]
    # The legacy column is left intact — Unit 2b still reads it until the resolver cuts over.
    assert conn.execute("SELECT key_verifier FROM relay_users WHERE name='user'").fetchone()[0] == "verifier-a"
    conn.close()


def test_backfill_carries_the_accounts_active_flag(tmp_path):
    """A revoked account's migrated credential is created INACTIVE, not active.

    Why this matters: the backfill must not resurrect access. A revoked account's key is dead
    today (the resolver checks relay_users.active), and after the cutover the credential's own
    `active` flag is what carries that — so copying it as active=1 would silently un-revoke a
    key the admin deliberately killed.
    """
    db = tmp_path / "old.sqlite3"
    _seed_pre_revamp_db(db, [("live", "v-live", "viewer", 1), ("revoked", "v-dead", "viewer", 0)])

    conn = open_relay_store(db)
    rows = dict(
        conn.execute(
            "SELECT u.name, c.active FROM relay_credentials c "
            "JOIN relay_users u ON u.id = c.user_id"
        ).fetchall()
    )
    assert rows == {"live": 1, "revoked": 0}
    conn.close()


def test_backfill_is_idempotent_across_repeated_opens(tmp_path):
    """Reopening the store many times never duplicates a credential.

    Why this matters: open_relay_store is called on EVERY request, not once at boot. A migration
    that appended a row per open would grow without bound and break the "one active label per
    account" index the moment a second credential is added.
    """
    db = tmp_path / "old.sqlite3"
    _seed_pre_revamp_db(db, [("user", "verifier-a", "admin", 1)])

    for _ in range(5):
        open_relay_store(db).close()

    conn = open_relay_store(db)
    assert conn.execute("SELECT COUNT(*) FROM relay_credentials").fetchone()[0] == 1
    conn.close()


def test_two_concurrent_opens_race_cleanly(tmp_path):
    """Two threads opening a pre-revamp DB at once produce exactly ONE credential per account.

    Why this matters: the relay opens the store per request, so a redeploy under live traffic can
    genuinely run this migration twice at the same instant. Without the serialized transaction,
    both workers would read "no credential exists" and both insert — and the UNIQUE key-verifier
    index would turn that into a 500 on a real request. This is the amendment-2 race, tested
    rather than assumed.
    """
    db = tmp_path / "old.sqlite3"
    _seed_pre_revamp_db(db, [("a", "v-a", "admin", 1), ("b", "v-b", "viewer", 1)])

    errors = []
    barrier = threading.Barrier(2)

    def open_it():
        try:
            barrier.wait()  # maximize the overlap — both threads hit the migration together
            open_relay_store(db).close()
        except Exception as exc:  # noqa: BLE001 — the test's job is to surface ANY failure
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    conn = open_relay_store(db)
    assert conn.execute("SELECT COUNT(*) FROM relay_credentials").fetchone()[0] == 2
    conn.close()


def test_backfill_recovers_when_interrupted_before_it_ran(tmp_path):
    """A DB where the table exists but the copy never happened is finished on the next open.

    Why this matters: the crash-mid-migration case. If the process died between creating
    relay_credentials and committing the backfill, the next open must complete the work rather
    than treating the table's existence as proof the migration is done. The transaction makes
    the copy all-or-nothing, so "interrupted" always looks exactly like this state.
    """
    db = tmp_path / "old.sqlite3"
    _seed_pre_revamp_db(db, [("user", "verifier-a", "admin", 1)])

    # Simulate the interruption: create the table (as a crashed open would have) but no rows.
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS relay_credentials ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, type TEXT NOT NULL,"
        " label TEXT NOT NULL, verifier TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT NOT NULL, last_used_at TEXT);"
    )
    conn.commit()
    conn.close()

    conn = open_relay_store(db)
    assert conn.execute("SELECT verifier FROM relay_credentials").fetchone()[0] == "verifier-a"
    conn.close()


def test_backfill_refuses_to_serve_a_half_migrated_store(tmp_path, monkeypatch):
    """If the copy silently did nothing, the open RAISES rather than returning a connection.

    Why this matters: the fail-closed invariant. A half-migrated store is the dangerous state —
    once Unit 2b's resolver reads credentials, an account with no credential row stops
    authenticating, and a working key starts returning 401 with nothing to explain it. Turning
    that into a loud startup failure is the whole point of checking a postcondition at all
    instead of trusting the INSERT's own rowcount.

    The setup makes the postcondition report work still outstanding after the copy ran — the
    observable shape of ANY way the copy could fail to land (a partial write, a silently
    swallowed statement, a future edit that breaks the WHERE clause). We drive the guard
    rather than one specific cause of it firing.
    """
    db = tmp_path / "old.sqlite3"
    _seed_pre_revamp_db(db, [("user", "verifier-a", "admin", 1)])

    real_count = store._accounts_missing_key_credentials
    calls = []

    def count_that_never_reaches_zero(conn):
        calls.append(1)
        # First call is the pre-check (real answer: work to do). Every later call is the
        # postcondition, which we force to keep reporting an unmigrated account.
        return real_count(conn) if len(calls) == 1 else 1

    monkeypatch.setattr(store, "_accounts_missing_key_credentials", count_that_never_reaches_zero)

    with pytest.raises(RelayStoreMigrationError, match="without a key credential"):
        open_relay_store(db)


def test_add_user_writes_the_verifier_only_to_the_credential(tmp_path):
    """A new account's real verifier goes ONLY to relay_credentials; the legacy column gets a sentinel.

    Why this matters: Unit 2b retires `relay_users.key_verifier`. The column is NOT NULL and
    cannot be dropped under the additive-migration idiom, so it must hold something — and that
    something must be inert. The sentinel is generated from uuid4, independent of the account's
    actual key, so it is not a hash of the key and not derivable from it.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    user_id = add_user(conn, "user", "verifier-a", "admin", [], "test", "2026-07-19T00:00:00+00:00")

    legacy = conn.execute("SELECT key_verifier FROM relay_users WHERE id=?", (user_id,)).fetchone()[0]
    assert legacy != "verifier-a"          # the real verifier is NOT here
    assert legacy.startswith("retired-unused-column-")
    cred = get_credential_by_key_verifier(conn, "verifier-a")
    assert cred is not None and cred["user_id"] == user_id and cred["type"] == "key"


def test_sentinels_are_unique_across_accounts(tmp_path):
    """Two accounts get DIFFERENT sentinels, so the legacy UNIQUE constraint still holds.

    Why this matters: a single shared constant would make the second `add_user` fail with an
    IntegrityError on the legacy column's UNIQUE index — provisioning would break at the second
    account, which is exactly when nobody is looking.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    add_user(conn, "a", "v-a", "admin", [], "test", "2026-07-19T00:00:00+00:00")
    add_user(conn, "b", "v-b", "viewer", [], "test", "2026-07-19T00:00:00+00:00")

    sentinels = {r[0] for r in conn.execute("SELECT key_verifier FROM relay_users")}
    assert len(sentinels) == 2


def test_delete_user_removes_credentials_so_the_key_can_be_re_added(tmp_path):
    """Deleting an account frees its key verifier for re-use.

    Why this matters: key verifiers are UNIQUE across the credentials table. An orphaned row
    would keep a deleted account's verifier reserved forever, so re-provisioning the same key
    (or, more likely, a test or a re-add after a mistake) would fail with an IntegrityError far
    from its cause. `delete_user` already frees the UNIQUE name; it must free this too.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    user_id = add_user(conn, "user", "verifier-a", "admin", [], "test", "2026-07-19T00:00:00+00:00")
    delete_user(conn, user_id)

    assert conn.execute("SELECT COUNT(*) FROM relay_credentials").fetchone()[0] == 0
    # The whole point: the same key can be provisioned again without an IntegrityError.
    add_user(conn, "user", "verifier-a", "admin", [], "test", "2026-07-19T00:00:00+00:00")


def test_revoking_an_account_deactivates_its_credentials(tmp_path):
    """Revoking an account deactivates every credential beneath it.

    Why this matters: "the account is revoked" and "none of its credentials work" must never be
    able to disagree. Nothing reads the credentials table yet, so this cannot change behavior in
    this unit — it prevents a live credential row sitting under a revoked account, primed to
    start authenticating the moment Unit 2b moves the resolver over.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    user_id = add_user(conn, "user", "verifier-a", "admin", [], "test", "2026-07-19T00:00:00+00:00")
    revoke_user(conn, user_id)

    assert get_credential_by_key_verifier(conn, "verifier-a") is None  # excluded: inactive
    assert conn.execute("SELECT active FROM relay_credentials").fetchone()[0] == 0


def test_only_one_active_password_per_account_is_possible(tmp_path):
    """A second active password for one account is refused by the DB, not by application code.

    Why this matters: amendment 7. Two admins setting a password concurrently would both read
    "no active password" and both insert, leaving an account with two valid passwords and no
    defined answer for which one Unit 3 verifies against. The partial unique index makes that
    unrepresentable rather than merely unlikely.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    user_id = add_user(conn, "user", "verifier-a", "admin", [], "test", "2026-07-19T00:00:00+00:00")
    add_credential(conn, user_id, "password", "login", "argon2-hash-1", "2026-07-19T00:00:00+00:00")

    try:
        add_credential(conn, user_id, "password", "login2", "argon2-hash-2", "2026-07-19T00:00:00+00:00")
        raise AssertionError("a second active password must not be insertable")
    except sqlite3.IntegrityError:
        pass

    # But revoking the first frees the slot — replacing a password is a normal act.
    first = get_active_password_credential(conn, user_id)
    assert revoke_credential(conn, first["id"]) is True
    add_credential(conn, user_id, "password", "login2", "argon2-hash-2", "2026-07-19T00:00:00+00:00")
    assert get_active_password_credential(conn, user_id)["verifier"] == "argon2-hash-2"


def test_two_keys_on_one_account_revoke_independently(tmp_path):
    """The two-machine case: two keys under one account, revoking one leaves the other working.

    Why this matters: this is the concrete thing the whole revamp exists to enable — one identity
    holding a Mac key and a WSL2 key, where losing a laptop revokes ONE credential instead of
    killing the identity. Revocation targets the credential id, and must not touch its sibling.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    user_id = add_user(conn, "user", "v-mac", "contributor", ["demo"], "test", "2026-07-19T00:00:00+00:00")
    wsl_id = add_credential(conn, user_id, "key", "wsl2", "v-wsl", "2026-07-19T00:00:00+00:00")

    assert revoke_credential(conn, wsl_id) is True
    assert get_credential_by_key_verifier(conn, "v-wsl") is None       # revoked
    assert get_credential_by_key_verifier(conn, "v-mac") is not None   # untouched
    # Revoking again reports no change, so a caller can 404 instead of faking success.
    assert revoke_credential(conn, wsl_id) is False


def test_list_credentials_never_exposes_a_verifier(tmp_path):
    """The credential listing omits verifiers entirely.

    Why this matters: the same rule `list_users` follows — a verifier never leaves the store. A
    listing is the easiest place to leak one by accident, since it is the one accessor whose
    whole job is to be displayed to a human.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    user_id = add_user(conn, "user", "v-mac", "admin", [], "test", "2026-07-19T00:00:00+00:00")
    add_credential(conn, user_id, "key", "wsl2", "v-wsl", "2026-07-19T00:00:00+00:00")

    listed = list_credentials(conn, user_id)
    assert len(listed) == 2
    assert all("verifier" not in cred for cred in listed)
    assert {cred["label"] for cred in listed} == {"default", "wsl2"}


def test_get_missing_id_returns_none(tmp_path):
    """Fetching an id that does not exist returns None, not an error.

    Why this matters: a stale or hand-typed /report/<id> link is an expected case.
    Returning None lets the server render a clean 404 instead of crashing — the
    behavior CP7's 404 test depends on.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert get(conn, 999) is None


def test_history_returns_a_projects_reports_newest_first(tmp_path):
    """history() returns only the named project's reports, ordered newest first.

    Why this matters: the per-project history view shows the most recent update at
    the top and must not bleed in another project's reports. We ingest two reports
    for 'demo' (different timestamps) and one for 'other', then check demo's history
    is exactly its two, newest first.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    ingest(conn, _blob("demo", generated_at="2026-06-18T08:00:00+00:00"), "2026-06-18T08:00:01+00:00")
    ingest(conn, _blob("demo", generated_at="2026-06-18T09:00:00+00:00"), "2026-06-18T09:00:01+00:00")
    ingest(conn, _blob("other", generated_at="2026-06-18T10:00:00+00:00"), "2026-06-18T10:00:01+00:00")

    demo_history = history(conn, "demo")

    assert [r["project"] for r in demo_history] == ["demo", "demo"]  # no 'other'
    # Newest generated_at first.
    assert demo_history[0]["generated_at"] == "2026-06-18T09:00:00+00:00"
    assert demo_history[1]["generated_at"] == "2026-06-18T08:00:00+00:00"


def test_history_for_unknown_project_is_empty(tmp_path):
    """history() for a project with no reports is an empty list.

    Why this matters: the dashboard may be asked for a project that has never
    pushed; an empty list (not an error) lets the view show a clean empty state.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert history(conn, "never-seen") == []


def test_list_projects_counts_and_orders_by_latest(tmp_path):
    """list_projects() summarizes each project with a count and its latest time,
    most-recently-active first.

    Why this matters: this backs the dashboard index. We ingest two reports for
    'alpha' and one (more recent) for 'beta', and check the counts are right and
    that 'beta' — active most recently — sorts ahead of 'alpha'.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    ingest(conn, _blob("alpha", generated_at="2026-06-18T08:00:00+00:00"), "2026-06-18T08:00:01+00:00")
    ingest(conn, _blob("alpha", generated_at="2026-06-18T09:00:00+00:00"), "2026-06-18T09:00:01+00:00")
    ingest(conn, _blob("beta", generated_at="2026-06-18T12:00:00+00:00"), "2026-06-18T12:00:01+00:00")

    projects = list_projects(conn)

    assert [p["project"] for p in projects] == ["beta", "alpha"]  # beta most recent
    by_name = {p["project"]: p for p in projects}
    assert by_name["alpha"]["report_count"] == 2
    assert by_name["beta"]["report_count"] == 1
    assert by_name["alpha"]["latest_generated_at"] == "2026-06-18T09:00:00+00:00"


def test_latest_report_per_project_carries_count_and_latest_body(tmp_path):
    """latest_report_per_project() returns each project's newest report id+body, ordered.

    Why this matters: this backs the portfolio HOME. Unlike list_projects (count +
    timestamp only), it must also surface the LATEST report's id and body so the home can
    show a one-line headline and link to that report. We ingest two reports for 'alpha'
    (varying the body so we can tell which one is returned) and one more-recent for 'beta',
    then check the counts, the project order (most-recent first), and that 'alpha' carries
    its SECOND report's body — the newest by generated_at.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    a1 = _blob("alpha", generated_at="2026-06-18T08:00:00+00:00")
    a1["body"] = "First alpha update."
    ingest(conn, a1, "2026-06-18T08:00:01+00:00")
    a2 = _blob("alpha", generated_at="2026-06-18T09:00:00+00:00")
    a2["body"] = "Second alpha update."
    latest_alpha_id = ingest(conn, a2, "2026-06-18T09:00:01+00:00")
    b1 = _blob("beta", generated_at="2026-06-18T12:00:00+00:00")
    b1["body"] = "Beta update."
    ingest(conn, b1, "2026-06-18T12:00:01+00:00")

    rows = latest_report_per_project(conn)

    assert [r["project"] for r in rows] == ["beta", "alpha"]  # beta most recent
    by_name = {r["project"]: r for r in rows}
    assert by_name["alpha"]["report_count"] == 2
    assert by_name["beta"]["report_count"] == 1
    # alpha resolves to its NEWEST report (by generated_at) — id and body both.
    assert by_name["alpha"]["latest_report_id"] == latest_alpha_id
    assert by_name["alpha"]["latest_body"] == "Second alpha update."
    assert by_name["alpha"]["latest_generated_at"] == "2026-06-18T09:00:00+00:00"


def test_latest_report_per_project_matches_history_when_ingest_order_differs(tmp_path):
    """The "latest" report agrees with history()[0] even if a later-ingested report is OLDER.

    Why this matters: "newest" must mean newest by generated_at (tie-broken by id), the
    SAME rule history() uses — not simply the last-ingested row. Here a backfilled report
    (ingested second, but with an EARLIER generated_at) must NOT win. If the query used
    MAX(id) it would pick the backfill and disagree with the project page's history()[0];
    this pins that they stay consistent.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # Ingested FIRST but generated LATER — this is the true newest.
    newer = _blob("demo", generated_at="2026-06-18T10:00:00+00:00")
    newer["body"] = "The newest update."
    ingest(conn, newer, "2026-06-18T10:00:01+00:00")
    # Ingested SECOND but generated EARLIER (a backfill) — must NOT be chosen as latest.
    older = _blob("demo", generated_at="2026-06-18T07:00:00+00:00")
    older["body"] = "An older, backfilled update."
    ingest(conn, older, "2026-06-18T11:00:00+00:00")

    rows = latest_report_per_project(conn)
    assert len(rows) == 1
    # Same report history() would return first — consistency between the two views.
    assert rows[0]["latest_body"] == history(conn, "demo")[0]["body"]
    assert rows[0]["latest_body"] == "The newest update."


def test_latest_report_per_project_empty_store_is_empty(tmp_path):
    """An empty store yields an empty list (a fresh relay's portfolio shows nothing).

    Why this matters: a brand-new relay with no pushes must render a clean empty-state,
    not error — so the helper returns [] rather than raising.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert latest_report_per_project(conn) == []


# --- E2 Inc 2: the live checklist (current state, one row per project) ----------
# upsert_checklist REPLACES (not appends), get_checklist decodes back to dicts, and
# latest_report_per_project carries precomputed done/total counts for the card badge.


def _items(*pairs):
    """Build a checklist as a list of {text, done} dicts.

    Why: the checklist's wire/stored shape is named objects; a tiny helper keeps each
    test to the items it cares about. Each arg is a (text, done) tuple.
    """
    return [{"text": text, "done": done} for text, done in pairs]


def test_upsert_checklist_replaces_not_appends(tmp_path):
    """A second upsert overwrites the project's checklist rather than adding a row.

    Why this matters: the live checklist is CURRENT STATE — one row per project. If
    upsert appended, get_checklist would return stale items, and the badge would
    double-count. We upsert twice and assert only the second push survives.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(conn, "demo", _items(("Old item", True)), "2026-06-25T00:00:00+00:00")
    upsert_checklist(
        conn,
        "demo",
        _items(("New A", False), ("New B", True)),
        "2026-06-25T01:00:00+00:00",
    )

    assert get_checklist(conn, "demo") == _items(("New A", False), ("New B", True))


def test_get_checklist_missing_project_returns_none(tmp_path):
    """A project with no checklist row returns None, not an error or empty list.

    Why this matters: None means "this project never had a checklist" (feature off),
    which the renderer reads as "omit the block". It must be distinct from [] below.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert get_checklist(conn, "demo") is None


def test_get_checklist_empty_list_is_distinct_from_none(tmp_path):
    """An upserted empty checklist returns [] — distinct from a missing project's None.

    Why this matters: an enabled-but-empty checklist ([]) legitimately clears a
    project's prior list. The store must preserve the [] vs None distinction so the
    renderer can tell "checklist enabled, no items" from "no checklist at all".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(conn, "demo", [], "2026-06-25T00:00:00+00:00")
    assert get_checklist(conn, "demo") == []


def test_producer_checklists_are_keyed_per_producer_and_replace_in_place(tmp_path):
    """Two producers' checklists coexist; a producer's re-push replaces ONLY its own row.

    Why this matters: the per-producer store is keyed (project, author_id), so two contributors
    on the same project keep independent checklists (that is the whole point of the cards), and
    a producer pushing again overwrites its own row without touching the other's.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # producer_checklists_for INNER JOINs relay_users (active only), so the producers must be
    # real active users; add_user returns their autoincrement ids.
    b_id = add_user(conn, "Teammate B", "vb", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    c_id = add_user(conn, "Teammate C", "vc", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    upsert_producer_checklist(
        conn, "demo", b_id, "Teammate B", _items(("A", True)), "2026-06-25T00:00:00+00:00"
    )
    upsert_producer_checklist(
        conn, "demo", c_id, "Teammate C", _items(("B", False)), "2026-06-25T01:00:00+00:00"
    )
    # Producer B re-pushes a new checklist — replaces its own row only.
    upsert_producer_checklist(
        conn, "demo", b_id, "Teammate B", _items(("A2", False)), "2026-06-25T02:00:00+00:00"
    )

    got = producer_checklists_for(conn, "demo")
    assert [(p["author_id"], p["author_name"]) for p in got] == [
        (b_id, "Teammate B"),
        (c_id, "Teammate C"),
    ]  # ordered by name; both present
    by_id = {p["author_id"]: p for p in got}
    assert [i["text"] for i in by_id[b_id]["items"]] == ["A2"]  # B's row replaced
    assert [i["text"] for i in by_id[c_id]["items"]] == ["B"]  # C's row untouched


def test_producer_checklists_excludes_revoked_producers(tmp_path):
    """A revoked contributor's live checklist card disappears (current state, not history).

    Why this matters: unlike a discussion author_name (a historical utterance that keeps its
    author across revocation), a per-producer checklist is CURRENT state — a revoked contributor
    is off the project, so their stale card must not show. We push two producers, revoke one, and
    assert only the active producer's card remains.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    b_id = add_user(conn, "Teammate B", "vb", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    c_id = add_user(conn, "Teammate C", "vc", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    upsert_producer_checklist(
        conn, "demo", b_id, "Teammate B", _items(("A", True)), "2026-06-25T00:00:00+00:00"
    )
    upsert_producer_checklist(
        conn, "demo", c_id, "Teammate C", _items(("B", False)), "2026-06-25T00:00:00+00:00"
    )
    revoke_user(conn, c_id)  # C leaves the project

    got = producer_checklists_for(conn, "demo")
    assert [p["author_name"] for p in got] == ["Teammate B"]  # revoked C's stale card is gone


def test_producer_checklists_resolve_an_agents_effective_producer(tmp_path):
    """An agent's row carries its OPERATOR as the effective producer; a human is its own.

    Why this matters: this is the store half of Unit 4b's fold. Resolving it here — in the
    one query that already joins relay_users for the active filter — is what lets the
    serializer group cards without a second lookup.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    human_id = add_user(conn, "yoo", "v1", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    agent_id = add_user(
        conn, "claude-mac", "v2", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00",
        account_kind="agent", operated_by=human_id,
    )
    for producer_id, name in ((human_id, "yoo"), (agent_id, "claude-mac")):
        upsert_producer_checklist(
            conn, "demo", producer_id, name, _items(("A", True)), "2026-06-25T00:00:00+00:00"
        )

    rows = {p["author_name"]: p for p in producer_checklists_for(conn, "demo")}
    # The agent folds into the human it acts for...
    assert rows["claude-mac"]["effective_producer_id"] == human_id
    assert rows["claude-mac"]["effective_producer_name"] == "yoo"
    # ...while the human is its own effective producer (so a no-agents project is unchanged).
    assert rows["yoo"]["effective_producer_id"] == human_id
    assert rows["yoo"]["effective_producer_name"] == "yoo"


def test_renaming_an_operator_regroups_its_agents_cards(tmp_path):
    """The operator name comes from the LIVE account, so a rename regroups immediately.

    Note the deliberate asymmetry this pins: `author_name` stays the row's DENORMALIZED value
    (what the producer was called when it pushed), while the effective producer name is
    resolved live — matching how 4a's report attribution behaves under a rename.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    human_id = add_user(conn, "yoo", "v1", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    agent_id = add_user(
        conn, "claude-mac", "v2", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00",
        account_kind="agent", operated_by=human_id,
    )
    upsert_producer_checklist(
        conn, "demo", agent_id, "claude-mac", _items(("A", True)), "2026-06-25T00:00:00+00:00"
    )
    rename_user(conn, human_id, "yoo-renamed")

    (row,) = producer_checklists_for(conn, "demo")
    assert row["effective_producer_name"] == "yoo-renamed"  # live account, regrouped
    assert row["author_name"] == "claude-mac"  # the push's own recorded name is untouched


def test_revoking_an_agent_removes_only_its_own_card(tmp_path):
    """A revoked agent's row drops out; its operator's own card survives.

    The mirror of the no-silent-cascade rule from 4a: revoking one identity must not take
    down the other's current state.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    human_id = add_user(conn, "yoo", "v1", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    agent_id = add_user(
        conn, "claude-mac", "v2", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00",
        account_kind="agent", operated_by=human_id,
    )
    for producer_id, name in ((human_id, "yoo"), (agent_id, "claude-mac")):
        upsert_producer_checklist(
            conn, "demo", producer_id, name, _items(("A", True)), "2026-06-25T00:00:00+00:00"
        )
    revoke_user(conn, agent_id)

    assert [p["author_name"] for p in producer_checklists_for(conn, "demo")] == ["yoo"]


def test_revoking_an_operator_leaves_its_agents_card_grouped_under_it(tmp_path):
    """A revoked OPERATOR keeps grouping its still-active agent's card.

    The active filter is an INNER JOIN on the PRODUCER's own account, not the operator's, so
    a revoked operator does not silently erase work its agent is still doing. This mirrors 4a
    (revoking an operator does not revoke its agents) — the card stays visible under the
    operator's name, which is the honest reading: that work was done on their behalf.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    human_id = add_user(conn, "yoo", "v1", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    agent_id = add_user(
        conn, "claude-mac", "v2", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00",
        account_kind="agent", operated_by=human_id,
    )
    upsert_producer_checklist(
        conn, "demo", agent_id, "claude-mac", _items(("A", True)), "2026-06-25T00:00:00+00:00"
    )
    revoke_user(conn, human_id)

    (row,) = producer_checklists_for(conn, "demo")
    assert row["effective_producer_name"] == "yoo"


def test_producer_checklists_for_unknown_project_is_empty(tmp_path):
    """A project with no per-producer checklists returns [] (legacy-only / never pushed).

    Why this matters: a legacy-only or single-writer project has no per-producer rows, and the
    serializer must get a clean empty list so the SPA simply omits the per-producer section.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert producer_checklists_for(conn, "never-seen") == []


def test_producer_checklists_for_carries_updated_at(tmp_path):
    """Each producer row now surfaces its push time, which the effective-checklist merge needs.

    Why this matters: the merge orders producers by updated_at for last-writer-per-item metadata
    (C3 Inc 2.5). If the store dropped the column, the merge would have no timestamp to fold on.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    b_id = add_user(conn, "Teammate B", "vb", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    upsert_producer_checklist(
        conn, "demo", b_id, "Teammate B", _items(("A", True)), "2026-06-25T09:30:00+00:00"
    )
    (row,) = producer_checklists_for(conn, "demo")
    assert row["updated_at"] == "2026-06-25T09:30:00+00:00"


# --- effective_checklist(): the read-side entry point for KI-30 ----------------------


def _add_producer_checklist(conn, project, name, verifier, items, updated_at):
    """Add an active contributor and push their per-producer checklist in one step.

    Why: every effective-checklist case needs ≥1 real active user (producer_checklists_for
    INNER JOINs relay_users); this collapses the add_user + upsert_producer_checklist boilerplate
    so each test reads as "these producers pushed these items". Returns the new user's id.
    """
    uid = add_user(conn, name, verifier, "contributor", [project], "test", "2026-06-25T00:00:00+00:00")
    upsert_producer_checklist(conn, project, uid, name, items, updated_at)
    return uid


def test_effective_checklist_ors_done_across_two_producers(tmp_path):
    """With ≥2 active producers the badge reads the merged (done-OR) checklist, not the aggregate.

    Scenario: the aggregate row (last-writer-wins) currently shows "Ship" NOT done, but producer
    B pushed it done. The effective checklist OR-s across producers, so "Ship" reads done — this
    is KI-30's fix at the store's read seam.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # Aggregate reflects whoever pushed last (here: not-done). It must be overridden by the merge.
    upsert_checklist(conn, "demo", _items(("Ship", False)), "2026-06-26T11:00:00+00:00")
    _add_producer_checklist(conn, "demo", "Teammate B", "vb", _items(("Ship", True)), "2026-06-26T10:00:00+00:00")
    _add_producer_checklist(conn, "demo", "Teammate C", "vc", _items(("Ship", False)), "2026-06-26T11:00:00+00:00")

    effective = effective_checklist(conn, "demo")
    assert [(i["text"], i["done"]) for i in effective] == [("Ship", True)]


def test_effective_checklist_single_producer_falls_back_to_aggregate(tmp_path):
    """With only one active producer the aggregate is returned unchanged (byte-identical fallback).

    Why this matters: a single-writer project must behave exactly as before this slice — no merge,
    the aggregate the sole source. Here the aggregate shows not-done and the lone producer shows
    done, and the fallback returns the aggregate (not-done).
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(conn, "demo", _items(("Ship", False)), "2026-06-26T11:00:00+00:00")
    _add_producer_checklist(conn, "demo", "Teammate B", "vb", _items(("Ship", True)), "2026-06-26T10:00:00+00:00")

    assert effective_checklist(conn, "demo") == _items(("Ship", False))  # the aggregate, unchanged


def test_effective_checklist_none_when_no_checklist_row(tmp_path):
    """A project with no aggregate checklist row resolves to None (existence-hiding preserved).

    Why this matters: the project route 404s on `checklist is None`; the effective wrapper must
    not turn a missing checklist into [].
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert effective_checklist(conn, "never-seen") is None


def test_effective_checklist_excludes_revoked_producer_from_the_merge(tmp_path):
    """A revoked producer's stale copy drops out of the merge (only active producers count).

    Scenario: A and B share "Ship"; revoked C carries a unique "Ghost" item. producer_checklists_for
    excludes C, so the merge runs over A and B only — "Ghost" never appears in the effective list,
    and a revoked producer's stale state can neither add phantom items nor sway done.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(conn, "demo", _items(("Ship", False)), "2026-06-26T12:00:00+00:00")
    _add_producer_checklist(conn, "demo", "Teammate A", "va", _items(("Ship", True)), "2026-06-26T10:00:00+00:00")
    _add_producer_checklist(conn, "demo", "Teammate B", "vb", _items(("Ship", False)), "2026-06-26T11:00:00+00:00")
    c_id = _add_producer_checklist(conn, "demo", "Teammate C", "vc", _items(("Ghost", True)), "2026-06-26T12:00:00+00:00")
    revoke_user(conn, c_id)

    effective = effective_checklist(conn, "demo")
    assert [i["text"] for i in effective] == ["Ship"]  # C's "Ghost" excluded; merge over A,B only
    assert effective[0]["done"] is True  # A said done → OR-ed done


def test_latest_report_per_project_badge_counts_reflect_the_merge(tmp_path):
    """The portfolio badge's precomputed done count uses the effective checklist at ≥2 producers.

    Scenario: two producers each track two items; between them all four distinct items are done
    at least once (A: X done, Y open; B: X open, Y done). The merged checklist has both done, so
    checklist_done == checklist_total == 2 — even though neither producer's own copy is fully done
    and the aggregate (last-writer) would show only one done.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # Aggregate = B's push (last writer): X open, Y done → would show 1/2 done on its own.
    upsert_checklist(conn, "demo", _items(("X", False), ("Y", True)), "2026-06-26T11:00:00+00:00")
    _add_producer_checklist(conn, "demo", "Teammate A", "va", _items(("X", True), ("Y", False)), "2026-06-26T10:00:00+00:00")
    _add_producer_checklist(conn, "demo", "Teammate B", "vb", _items(("X", False), ("Y", True)), "2026-06-26T11:00:00+00:00")

    row = {r["project"]: r for r in latest_report_per_project(conn)}["demo"]
    assert row["checklist_total"] == 2
    assert row["checklist_done"] == 2  # merged done-OR, not the aggregate's 1


def test_latest_report_per_project_carries_checklist_counts(tmp_path):
    """The portfolio overview surfaces precomputed done/total per project.

    Why this matters: the portfolio card shows an "X/Y done" badge. The store
    precomputes the counts (decoding the JSON once) so the renderer just presents
    them. A project with a checklist carries real counts; a project without one
    carries None for both, which the renderer reads as "omit the badge".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # 'alpha' has reports AND a checklist (2 of 3 done); 'beta' has reports only.
    ingest(conn, _blob("alpha", generated_at="2026-06-25T09:00:00+00:00"), "2026-06-25T09:00:01+00:00")
    upsert_checklist(
        conn,
        "alpha",
        _items(("Done one", True), ("Done two", True), ("Still open", False)),
        "2026-06-25T09:00:02+00:00",
    )
    ingest(conn, _blob("beta", generated_at="2026-06-25T08:00:00+00:00"), "2026-06-25T08:00:01+00:00")

    by_name = {r["project"]: r for r in latest_report_per_project(conn)}

    assert by_name["alpha"]["checklist_done"] == 2
    assert by_name["alpha"]["checklist_total"] == 3
    # No checklist row for beta → both None (badge omitted).
    assert by_name["beta"]["checklist_done"] is None
    assert by_name["beta"]["checklist_total"] is None


def test_latest_report_per_project_counts_at_risk_when_today_given(tmp_path):
    """With a reference date, the portfolio row carries the overdue/due-soon count (E2 Inc 3).

    Why this matters: the at-risk badge needs a precomputed count, derived against "today"
    in the display zone. The store reuses its single items decode to count alongside
    done/total, so the badge and the per-item render share one definition of "at risk".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn,
        "demo",
        [
            {"text": "Overdue", "done": False, "due_date": "2026-06-20"},
            {"text": "Due soon", "done": False, "due_date": "2026-06-28"},
            {"text": "Far off", "done": False, "due_date": "2026-12-01"},
            {"text": "Done past", "done": True, "due_date": "2026-06-01"},
        ],
        "2026-06-26T00:00:00+00:00",
    )

    row = {r["project"]: r for r in latest_report_per_project(conn, today=date(2026, 6, 26))}["demo"]

    # Overdue + due-soon = 2; the far-future and the done item are not at risk.
    assert row["checklist_at_risk"] == 2


def test_latest_report_per_project_nearest_milestone_when_today_given(tmp_path):
    """The portfolio row carries the soonest-due milestone group as a {group, nearest_due} hint.

    Why this matters: the home's "next milestone" line needs one precomputed group + date.
    Across two milestones, the one with the earliest OPEN deadline wins — here "Applications"
    (Jun 28) beats "Chores" (Aug 01) — and a done item's earlier date does not pull it in.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn,
        "demo",
        [
            {"text": "App A", "done": True, "due_date": "2026-06-10", "group": "Applications"},
            {"text": "App B", "done": False, "due_date": "2026-06-28", "group": "Applications"},
            {"text": "Chore", "done": False, "due_date": "2026-08-01", "group": "Chores"},
        ],
        "2026-06-26T00:00:00+00:00",
    )

    row = {r["project"]: r for r in latest_report_per_project(conn, today=date(2026, 6, 26))}["demo"]

    assert row["nearest_milestone"] == {"group": "Applications", "nearest_due": "2026-06-28"}


def test_latest_report_per_project_nearest_milestone_none_without_today(tmp_path):
    """Without a reference date (or no grouped+dated item), the hint is None.

    Why this matters: `today` gates the derivation like the other forward fields, and a
    checklist with no milestone (ungrouped) or no open deadline yields None so the card
    simply omits the line.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn, "demo", _items(("Open", False)), "2026-06-26T00:00:00+00:00"
    )

    no_today = {r["project"]: r for r in latest_report_per_project(conn)}["demo"]
    assert no_today["nearest_milestone"] is None

    # Even WITH today, an ungrouped checklist has no milestone → still None.
    with_today = {r["project"]: r for r in latest_report_per_project(conn, today=date(2026, 6, 26))}["demo"]
    assert with_today["nearest_milestone"] is None


def test_latest_report_per_project_at_risk_is_none_without_today(tmp_path):
    """Without a reference date, at-risk derivation is skipped → checklist_at_risk is None.

    Why this matters: `today` is the seam for the derivation. A caller that does not pass it
    (or a test of the existing done/total behavior) must be unaffected — the field is None,
    which the renderer reads as "omit the badge", not "0 at risk".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn, "demo", _items(("Open", False)), "2026-06-26T00:00:00+00:00"
    )

    row = {r["project"]: r for r in latest_report_per_project(conn)}["demo"]

    assert row["checklist_at_risk"] is None


def test_latest_report_per_project_includes_checklist_only_projects(tmp_path):
    """A project with a live checklist but ZERO reports still appears on the portfolio.

    Why this matters: this is the whole point of the fix. A dashboard-only project (a
    pushed checklist, no git, never report-ed — exactly the applications tracker) used
    to vanish from the home because the query ran FROM relay_reports, so family had no
    card to click. The row set is now project-driven (report OR checklist), so the
    checklist-only project shows up with report_count 0, None for every latest_* field,
    a populated checklist_updated_at to use as its last-activity time, and real
    done/total counts for the badge.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn,
        "applications",
        _items(("Applied to X", True), ("Heard back from Y", False)),
        "2026-06-25T10:00:00+00:00",
    )

    by_name = {r["project"]: r for r in latest_report_per_project(conn)}

    assert "applications" in by_name
    card = by_name["applications"]
    assert card["report_count"] == 0
    assert card["latest_report_id"] is None
    assert card["latest_body"] is None
    assert card["latest_generated_at"] is None
    assert card["checklist_updated_at"] == "2026-06-25T10:00:00+00:00"
    assert card["checklist_done"] == 1
    assert card["checklist_total"] == 2


def test_latest_report_per_project_interleaves_checklist_only_by_recency(tmp_path):
    """Checklist-only and report-bearing projects sort together by last activity.

    Why this matters: the home orders freshest-first across BOTH kinds. The order key is
    COALESCE(latest_generated_at, checklist_updated_at), so a checklist-only project with
    a fresher updated_at must outrank an older report, and an older checklist must fall
    below a newer report. We place a report between two checklist-only timestamps and
    assert the interleaving. A report+checklist project appears exactly once (UNION
    dedupe), keyed by its report time.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # Checklist-only, freshest.
    upsert_checklist(conn, "fresh_list", _items(("a", False)), "2026-06-25T12:00:00+00:00")
    # Report-only, middle.
    ingest(conn, _blob("mid_report", generated_at="2026-06-25T11:00:00+00:00"), "2026-06-25T11:00:01+00:00")
    # Checklist-only, oldest.
    upsert_checklist(conn, "old_list", _items(("b", True)), "2026-06-25T10:00:00+00:00")
    # Report+checklist on one project — must appear ONCE, keyed by its report time.
    ingest(conn, _blob("both", generated_at="2026-06-25T09:00:00+00:00"), "2026-06-25T09:00:01+00:00")
    upsert_checklist(conn, "both", _items(("c", False)), "2026-06-25T08:00:00+00:00")

    rows = latest_report_per_project(conn)
    assert [r["project"] for r in rows] == ["fresh_list", "mid_report", "old_list", "both"]
    # 'both' is deduped to a single row (UNION, not UNION ALL).
    assert [r["project"] for r in rows].count("both") == 1



# --- Supervisor-interaction loop: discussion items (E2 Inc 5, Unit 1) ----------
# These pin the append-only per-project discussion log the two-way loop builds on: that
# an entry round-trips with first-class, server-derived attribution (author_id/name/role),
# that the thread is ordered and project-scoped, that a NULL author_id (legacy-admin /
# machine post) survives, and that the since_id watermark advances correctly.


def test_add_discussion_item_round_trips_every_field(tmp_path):
    """An added discussion entry comes back from the read with every field intact.

    Why this matters: this is the discussion store's core promise — what the dashboard
    panel and the CLI pull render must equal what was posted. We add one entry with a
    real author_id and confirm all seven fields (id, project, author_id, author_name,
    role, body, created_at) survive the round-trip, since every consumer reads them.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")

    item_id = add_discussion_item(
        conn, "alpha", 7, "Supervisor A", "supervisor",
        "How's the auth slice going?", "2026-06-28T10:00:00+00:00",
    )

    items = discussion_items_for_project(conn, "alpha")
    assert len(items) == 1
    only = items[0]
    assert only["id"] == item_id
    assert only["project"] == "alpha"
    assert only["author_id"] == 7
    assert only["author_name"] == "Supervisor A"
    assert only["role"] == "supervisor"
    assert only["body"] == "How's the auth slice going?"
    assert only["created_at"] == "2026-06-28T10:00:00+00:00"


def test_discussion_items_are_oldest_first(tmp_path):
    """The read returns a project's items in chronological (insertion / id ASC) order.

    Why this matters: an append-only thread must read top-to-bottom in the order it was
    written, alternating supervisor and developer turns. We add three entries and assert
    they come back in insertion order, which the panel relies on to lay the thread out.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")

    add_discussion_item(conn, "alpha", 7, "Supervisor A", "supervisor", "first", "2026-06-28T10:00:00+00:00")
    add_discussion_item(conn, "alpha", None, "orion-cli", "developer", "second", "2026-06-28T11:00:00+00:00")
    add_discussion_item(conn, "alpha", 7, "Supervisor A", "supervisor", "third", "2026-06-28T12:00:00+00:00")

    bodies = [i["body"] for i in discussion_items_for_project(conn, "alpha")]
    assert bodies == ["first", "second", "third"]


def test_discussion_items_are_scoped_to_their_project(tmp_path):
    """The read returns only the named project's thread, never another's.

    Why this matters: each project has its own thread; an entry on project A must never
    leak into project B's. A thread needs no report to exist first — the project column
    IS the anchor — so we post straight to two projects and confirm
    each read sees only its own (also pinning that a thread needs no prior report).
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")

    add_discussion_item(conn, "alpha", 7, "Supervisor A", "supervisor", "on alpha", "2026-06-28T10:00:00+00:00")
    add_discussion_item(conn, "beta", 8, "Mum", "supervisor", "on beta", "2026-06-28T11:00:00+00:00")

    assert [i["body"] for i in discussion_items_for_project(conn, "alpha")] == ["on alpha"]
    assert [i["body"] for i in discussion_items_for_project(conn, "beta")] == ["on beta"]


def test_discussion_item_null_author_id_round_trips(tmp_path):
    """A NULL author_id is stored and read back as None (machine / legacy-admin post).

    Why this matters: the developer's CLI reply (Unit 3) and the legacy bootstrap admin
    have no relay_users row, so they post with author_id=None. That None must survive the
    round-trip rather than erroring or coercing to 0, since the renderer distinguishes
    "a registered user" from "a machine/legacy author" by this field.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")

    add_discussion_item(
        conn, "alpha", None, "orion-cli", "developer",
        "Auth slice landed.", "2026-06-28T12:00:00+00:00",
    )

    only = discussion_items_for_project(conn, "alpha")[0]
    assert only["author_id"] is None
    assert only["role"] == "developer"


def test_discussion_items_since_id_returns_only_newer(tmp_path):
    """since_id returns only items with a strictly greater id (the watermark cursor).

    Why this matters: this is the unread-cursor mechanism the developer's pull uses
    (Unit 3). After seeing up to id N, the next pull passes since_id=N and must get back
    only what came after — never a re-seen entry, never a skipped one. We add three and
    pull with since_id at the second's id, expecting only the third.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    d1 = add_discussion_item(conn, "demo", 7, "Supervisor A", "supervisor", "first", "2026-06-28T10:00:00+00:00")
    d2 = add_discussion_item(conn, "demo", None, "orion-cli", "developer", "second", "2026-06-28T11:00:00+00:00")
    d3 = add_discussion_item(conn, "demo", 7, "Supervisor A", "supervisor", "third", "2026-06-28T12:00:00+00:00")

    # since_id defaults to 0 → everything.
    assert [i["id"] for i in discussion_items_for_project(conn, "demo")] == [d1, d2, d3]
    # since_id = d2 → only the strictly-newer d3.
    newer = discussion_items_for_project(conn, "demo", since_id=d2)
    assert [i["body"] for i in newer] == ["third"]
    assert newer[0]["id"] == d3


def test_discussion_items_unknown_or_caught_up_is_empty(tmp_path):
    """An unknown project, or one with nothing newer than since_id, returns [].

    Why this matters: "no new messages" and "no such thread" both map to a clean empty
    list (not None, not an error), which the endpoint turns into a 200 empty response and
    the client reads as "nothing new". We check both: a never-seen project, and a real
    one pulled with since_id at its newest item.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    last = add_discussion_item(conn, "demo", 7, "Supervisor A", "supervisor", "only", "2026-06-28T10:00:00+00:00")

    assert discussion_items_for_project(conn, "never-seen") == []
    assert discussion_items_for_project(conn, "demo", since_id=last) == []


# --- Multi-party access: users, scope, revocation, audit (Increment 1) ---------
# These pin the per-user identity store the dashboard's auth/authZ build on: that a
# user + scope round-trip, that uniqueness is enforced, that the stateless-revocation
# levers (active + session_version) behave, and that a verifier never leaks via list.


def _store(tmp_path):
    """Open a fresh relay store (DRY across the user-store tests)."""
    return open_relay_store(tmp_path / "relay.sqlite3")


def test_add_user_round_trips_with_defaults_and_scope(tmp_path):
    """A provisioned user comes back with its fields, default flags, and project scope.

    Why this matters: this is the core identity record auth resolves on every login
    and request; the active/session_version DEFAULTS (1/1) and the scope rows must be
    exactly what add_user wrote.
    """
    conn = _store(tmp_path)
    uid = add_user(
        conn, "Alex", "verifier-alex", "viewer",
        ["orion", "incubator"], "admin-token", "2026-06-24T00:00:00+00:00",
    )
    row = get_user_by_id(conn, uid)
    assert row["name"] == "Alex"
    assert row["role"] == "viewer"
    assert row["active"] == 1            # default: active
    assert row["session_version"] == 1   # default: first session generation
    assert row["last_login_at"] is None  # not logged in yet
    # Scope round-trips, sorted.
    assert projects_for_user(conn, uid) == ["incubator", "orion"]


def test_lookups_miss_return_none(tmp_path):
    """Unknown credential/id/name are a clean None, never an error.

    Why this matters: a wrong key (no such credential) or a dead session (deleted id)
    must degrade to "not authenticated", which the server keys off a None.
    """
    conn = _store(tmp_path)
    assert get_credential_by_key_verifier(conn, "nope") is None
    assert get_user_by_id(conn, 999) is None
    assert get_user_by_name(conn, "ghost") is None


def test_duplicate_name_is_rejected(tmp_path):
    """A duplicate user name raises (UNIQUE), preventing an ambiguous second account.

    Why this matters: name is the admin's handle for revoke/list; two "Alex"es would
    make those ops ambiguous, so the store refuses it loudly.
    """
    conn = _store(tmp_path)
    add_user(conn, "Alex", "verifier-1", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, "Alex", "verifier-2", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")


def test_duplicate_verifier_is_rejected(tmp_path):
    """The same credential verifier cannot back two users (UNIQUE).

    Why this matters: a verifier is the login key's fingerprint; sharing it across
    users would make a single key authenticate as two identities.
    """
    conn = _store(tmp_path)
    add_user(conn, "Alex", "same-verifier", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, "Sam", "same-verifier", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")


def test_list_users_omits_verifier_and_includes_scope(tmp_path):
    """list_users returns scope + flags but NEVER the key verifier, ordered by name.

    Why this matters: an admin listing must never surface credential material (even
    hashed), and should show each user's scope. Ordering by name keeps it readable.
    """
    conn = _store(tmp_path)
    add_user(conn, "Sam", "verifier-sam", "viewer", ["orion"], "admin-token", "2026-06-24T00:00:00+00:00")
    add_user(conn, "Alex", "verifier-alex", "admin", [], "admin-token", "2026-06-24T00:00:00+00:00")
    users = list_users(conn)
    assert [u["name"] for u in users] == ["Alex", "Sam"]  # sorted by name
    assert all("key_verifier" not in u for u in users)    # verifier never exposed
    sam = next(u for u in users if u["name"] == "Sam")
    assert sam["projects"] == ["orion"] and sam["active"] is True


def test_bump_session_version_increments(tmp_path):
    """Bumping a user's session_version advances it (the stateless-logout lever).

    Why this matters: the signed cookie embeds the session_version it was minted with;
    bumping it is what makes an outstanding cookie stop validating on its next request.
    """
    conn = _store(tmp_path)
    uid = add_user(conn, "Alex", "verifier-alex", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    bump_session_version(conn, uid)
    assert get_user_by_id(conn, uid)["session_version"] == 2


def test_revoke_user_deactivates_and_bumps_version_atomically(tmp_path):
    """Revoking sets active=0 AND bumps session_version in one step.

    Why this matters: revocation must both deny a future login (active=0) and kill any
    live cookie (session_version bump); doing both leaves no window where one applies
    but not the other.
    """
    conn = _store(tmp_path)
    uid = add_user(conn, "Alex", "verifier-alex", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    revoke_user(conn, uid)
    row = get_user_by_id(conn, uid)
    assert row["active"] == 0 and row["session_version"] == 2


def test_update_last_login_stamps_the_time(tmp_path):
    """A login stamps last_login_at (an operational signal, not an auth gate).

    Why this matters: the admin can see who actually uses their access and spot stale
    grants; it starts NULL and is set on first login.
    """
    conn = _store(tmp_path)
    uid = add_user(conn, "Alex", "verifier-alex", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    update_last_login(conn, uid, "2026-06-24T12:00:00+00:00")
    assert get_user_by_id(conn, uid)["last_login_at"] == "2026-06-24T12:00:00+00:00"


def test_record_admin_audit_appends_a_row(tmp_path):
    """record_admin_audit writes an append-only trail row with JSON-encoded projects.

    Why this matters: the multi-party model needs accountability — who provisioned or
    revoked whom, with what scope — and it must be queryable after the fact.
    """
    conn = _store(tmp_path)
    record_admin_audit(
        conn, "admin-token", "create_user", "Alex", "viewer",
        ["orion", "incubator"], "2026-06-24T00:00:00+00:00",
    )
    row = conn.execute(
        "SELECT actor, action, target_user, role, projects FROM relay_admin_audit"
    ).fetchone()
    assert row["actor"] == "admin-token" and row["action"] == "create_user"
    assert row["target_user"] == "Alex" and row["role"] == "viewer"
    # projects is JSON-encoded for the TEXT column.
    import json
    assert json.loads(row["projects"]) == ["orion", "incubator"]


# --- observed-state history (E2 Inc 3 Unit 3 — the forward-store's "remember") -------


def _obs(text, done, due_date=None, key=None):
    """Build a checklist wire-item dict (the shape record_observations consumes).

    Why: mirrors the producer's optional-field shape — due_date / key present only when set
    — so tests exercise the real "key absent → fall back to text" path.
    """
    item = {"text": text, "done": done}
    if due_date is not None:
        item["due_date"] = due_date
    if key is not None:
        item["key"] = key
    return item


def test_record_observations_accumulates_across_pushes(tmp_path):
    """Each push APPENDS observation rows rather than replacing — history accumulates.

    Why this matters: this is the difference from relay_project_checklists (current state,
    upserted). The forward-store must keep every push's observation so slippage/history can
    be derived later. Two pushes of the same item yield two rows, oldest first.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(conn, "demo", [_obs("A", False, key="A")], "2026-06-25T00:00:00+00:00")
    record_observations(conn, "demo", [_obs("A", True, key="A")], "2026-06-26T00:00:00+00:00")

    hist = observed_history(conn, "demo")

    assert len(hist) == 2
    # Oldest first; done decoded back to a real bool.
    assert [h["done"] for h in hist] == [False, True]
    assert [h["observed_at"] for h in hist] == [
        "2026-06-25T00:00:00+00:00",
        "2026-06-26T00:00:00+00:00",
    ]


def test_observed_history_surfaces_author_id(tmp_path):
    """observed_history now carries each row's recording producer (C3 Inc 2.5).

    Why this matters: per-producer slippage partitions the stream by author_id, so the read
    side must surface it. An attributed push carries the producer's id; a legacy (anonymous)
    push carries None. Before this slice author_id was stamped but never read back out here.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(
        conn, "demo", [_obs("A", False, key="A")], "2026-06-25T00:00:00+00:00", author_id=7
    )
    record_observations(conn, "demo", [_obs("B", False, key="B")], "2026-06-26T00:00:00+00:00")

    hist = observed_history(conn, "demo")

    by_key = {h["item_key"]: h for h in hist}
    assert by_key["A"]["author_id"] == 7  # attributed push
    assert by_key["B"]["author_id"] is None  # legacy / anonymous push


def test_observed_item_key_stable_across_a_status_change(tmp_path):
    """A tracker application keeps ONE item_key as its status (and text) changes.

    Why this matters: the whole reason for a producer-emitted key. The application's text
    embeds status ("App - Not started" → "App - Submitted"), so a text-keyed store would see
    two different items and lose the deadline history. With key=title, both pushes land under
    the SAME item_key, so slippage (Unit 4) can track the one item — and see its deadline move
    later — across the status change.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(
        conn,
        "demo",
        [_obs("App - Not started", False, due_date="2026-07-01", key="App")],
        "2026-06-20T00:00:00+00:00",
    )
    record_observations(
        conn,
        "demo",
        [_obs("App - Submitted", True, due_date="2026-07-15", key="App")],
        "2026-06-26T00:00:00+00:00",
    )

    hist = observed_history(conn, "demo")

    # One identity across both pushes, not two — despite the text changing.
    assert {h["item_key"] for h in hist} == {"App"}
    # And the deadline's move-later is visible in time order (the slippage signal).
    assert [(h["due_date"], h["done"]) for h in hist] == [
        ("2026-07-01", False),
        ("2026-07-15", True),
    ]


def test_observed_history_rebuilds_current_state_as_latest_per_key(tmp_path):
    """Folding the history (newest observation per key) reconstructs the live checklist.

    Why this matters: the store is a PROJECTION — rebuildable from the append-only record.
    Taking the latest observation for each item_key must match the final pushed state, which
    is what makes "remember" a faithful downstream view rather than authored truth.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(
        conn,
        "demo",
        [_obs("A - Not started", False, key="A"), _obs("B", False)],
        "2026-06-25T00:00:00+00:00",
    )
    record_observations(
        conn,
        "demo",
        [_obs("A - Submitted", True, due_date="2026-07-01", key="A"), _obs("B", True)],
        "2026-06-26T00:00:00+00:00",
    )

    # Fold oldest→newest, so the last write per item_key wins (a simple projection).
    latest = {}
    for h in observed_history(conn, "demo"):
        latest[h["item_key"]] = (h["done"], h["due_date"])

    assert latest == {"A": (True, "2026-07-01"), "B": (True, None)}


def test_record_observations_falls_back_to_text_when_no_key(tmp_path):
    """An item with no `key` is identified by its text (tasks/table items are stable).

    Why this matters: only tracker applications need the separate key; a plain checkbox item
    carries no status in its text, so the store keys it by text — no producer change for the
    common case, mirroring KI-6's text identity.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(conn, "demo", [_obs("Plain task", False)], "2026-06-26T00:00:00+00:00")

    assert observed_history(conn, "demo")[0]["item_key"] == "Plain task"


def test_observed_history_empty_for_project_with_no_observations(tmp_path):
    """A project with no recorded observations returns an empty list, not an error.

    Why this matters: a project that has never pushed a checklist simply has no forward
    history; the read must degrade to [] so callers (and Unit 4's derivation) handle it cleanly.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert observed_history(conn, "demo") == []


def test_latest_report_per_project_counts_slipping_from_history(tmp_path):
    """The portfolio row carries a slipping count derived from the observation history.

    Why this matters: the "N slipping" badge needs a precomputed count. Two pushes postpone
    item A's deadline (2026-07-01 → 2026-07-15) — A is slipping — while the project's live
    checklist makes it appear in the portfolio set. The count must be 1.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    push1 = [{"text": "A - Not started", "done": False, "due_date": "2026-07-01", "key": "A"}]
    push2 = [{"text": "A - In progress", "done": False, "due_date": "2026-07-15", "key": "A"}]
    # Mirror the server: upsert the live checklist AND append observations on each push.
    upsert_checklist(conn, "demo", push1, "2026-06-20T00:00:00+00:00")
    record_observations(conn, "demo", push1, "2026-06-20T00:00:00+00:00")
    upsert_checklist(conn, "demo", push2, "2026-06-25T00:00:00+00:00")
    record_observations(conn, "demo", push2, "2026-06-25T00:00:00+00:00")

    row = {r["project"]: r for r in latest_report_per_project(conn, today=date(2026, 6, 26))}["demo"]

    assert row["checklist_slipping"] == 1


def test_latest_report_per_project_slipping_is_none_without_today(tmp_path):
    """Without a reference date, slippage derivation is skipped → checklist_slipping is None.

    Why this matters: `today` gates the derivation (mirroring at-risk). A caller that omits it
    (or a test of the existing counts) is unaffected — None reads as "omit the badge".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(conn, "demo", _items(("A", False)), "2026-06-26T00:00:00+00:00")

    row = {r["project"]: r for r in latest_report_per_project(conn)}["demo"]

    assert row["checklist_slipping"] is None


# --- E2 Inc 4: per-project kind (relay_project_meta) --------------------------------
# set_project_kind upserts, get_project_kind defaults to "project", and the portfolio
# query carries the kind so the home can split projects from trackers.


def test_get_project_kind_defaults_to_project(tmp_path):
    """A project with no meta row reads as "project" — the safe default.

    Why this matters: a report-only project (or one pushed by a producer predating the
    flag) never records a kind, so the default must be "project" rather than an error.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert get_project_kind(conn, "demo") == "project"


def test_set_project_kind_roundtrips_and_upserts(tmp_path):
    """set_project_kind stores the kind and a later call overwrites it (one row).

    Why this matters: kind is CURRENT STATE that rides every push; a re-push must replace,
    not accumulate, so the latest config value always wins.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    set_project_kind(conn, "apps", "tracker")
    assert get_project_kind(conn, "apps") == "tracker"
    # A later push flips it back — the upsert overwrites the same row.
    set_project_kind(conn, "apps", "project")
    assert get_project_kind(conn, "apps") == "project"


def test_latest_report_per_project_carries_kind(tmp_path):
    """The portfolio rows expose kind: "tracker" when set, "project" by default.

    Why this matters: the home splits projects from trackers off this field. A
    checklist-only project marked "tracker" must carry that kind; an unmarked project
    with a report must default to "project".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # A report-only project (unmarked → defaults to "project").
    ingest(conn, _blob(project="orion"), "2026-06-26T00:00:00+00:00")
    # A checklist-only project explicitly marked a tracker.
    upsert_checklist(conn, "apps", _items(("Apply", False)), "2026-06-26T00:00:00+00:00")
    set_project_kind(conn, "apps", "tracker")

    rows = {r["project"]: r for r in latest_report_per_project(conn, today=date(2026, 6, 26))}
    assert rows["orion"]["kind"] == "project"
    assert rows["apps"]["kind"] == "tracker"


# --- E1.2 (forward-look): per-project due_soon_days (relay_project_meta) ------------
# set_due_soon_days upserts the (nullable) horizon column, get_due_soon_days returns None
# when unset, and the portfolio at-risk count classifies against the per-project window.


def _dated_item(text, due_date, done=False):
    """Build one checklist item carrying a deadline (the store's wire shape)."""
    return {"text": text, "due_date": due_date, "done": done}


def test_get_due_soon_days_is_none_until_set(tmp_path):
    """A project with no horizon set reads as None — the omit-when-unset signal.

    Why this matters: None is what tells the classifier to fall back to the 7-day default,
    so a project that never configured the knob must read None, not 7 or an error. It must
    also be distinct from any real value so back-compat is unambiguous.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert get_due_soon_days(conn, "demo") is None


def test_set_due_soon_days_roundtrips_and_last_writer_wins(tmp_path):
    """set_due_soon_days stores the horizon and a later push overwrites it (one row).

    Why this matters: the horizon is CURRENT STATE that rides every push; two producers (or
    two edits) must resolve last-writer-wins, exactly like kind — the newest config value
    always wins, never accumulates.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    set_due_soon_days(conn, "apps", 14)
    assert get_due_soon_days(conn, "apps") == 14
    # A later push with a different value overwrites the same row (last writer wins).
    set_due_soon_days(conn, "apps", 30)
    assert get_due_soon_days(conn, "apps") == 30


def test_set_due_soon_days_none_clears_back_to_default(tmp_path):
    """Passing None writes NULL, so get returns None again — the set→unset round-trip.

    Why this matters: this is the store half of the fix for a stale horizon. Once set to 30,
    a producer that stops configuring the knob passes None, which must CLEAR the column (NULL)
    so get_due_soon_days reads None again and the serializers resolve it to the 7-day default —
    not leave 30 stuck forever.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    set_due_soon_days(conn, "apps", 30)
    assert get_due_soon_days(conn, "apps") == 30
    set_due_soon_days(conn, "apps", None)  # producer stopped setting it
    assert get_due_soon_days(conn, "apps") is None  # cleared → resolves to the default


def test_due_soon_days_and_kind_share_the_row_without_clobbering(tmp_path):
    """due_soon_days and kind are written independently on the shared meta row.

    Why this matters: the two knobs live in one relay_project_meta row but arrive/​persist
    separately. Setting one must not reset the other — a bare due_soon_days upsert must leave
    kind at its prior value (and vice versa), or a checklist push carrying only one field
    would silently wipe the other.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # Set kind first, then set the horizon: kind must survive the second, unrelated upsert.
    set_project_kind(conn, "apps", "tracker")
    set_due_soon_days(conn, "apps", 14)
    assert get_project_kind(conn, "apps") == "tracker"
    assert get_due_soon_days(conn, "apps") == 14
    # And the reverse: a horizon-only project defaults kind to "project" (schema default),
    # and a later kind push must not disturb the stored horizon.
    set_due_soon_days(conn, "only-horizon", 20)
    assert get_project_kind(conn, "only-horizon") == "project"
    set_project_kind(conn, "only-horizon", "tracker")
    assert get_due_soon_days(conn, "only-horizon") == 20


def test_latest_report_per_project_at_risk_honors_due_soon_days(tmp_path):
    """The portfolio at-risk count classifies against the project's due_soon_days.

    Why this matters: an item due in 10 days is at-risk under a 14-day horizon but NOT under
    the 7-day default. The portfolio badge must reflect the per-project window so it agrees
    with the per-item render, and an un-configured project must keep the 7-day behavior.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    today = date(2026, 6, 26)
    ten_days_out = "2026-07-06"  # today + 10 days
    # Two trackers with the SAME 10-day-out open item; only one sets a 14-day horizon.
    upsert_checklist(conn, "wide", [_dated_item("Ship", ten_days_out)], "2026-06-26T00:00:00+00:00")
    set_due_soon_days(conn, "wide", 14)
    upsert_checklist(conn, "default", [_dated_item("Ship", ten_days_out)], "2026-06-26T00:00:00+00:00")

    rows = {r["project"]: r for r in latest_report_per_project(conn, today=today)}
    # 10 <= 14 → at risk for the wide project; 10 > 7 → not at risk for the default one.
    assert rows["wide"]["checklist_at_risk"] == 1
    assert rows["wide"]["due_soon_days"] == 14
    assert rows["default"]["checklist_at_risk"] == 0
    assert rows["default"]["due_soon_days"] is None


# --- relay_project_disciplines: observed-principles current state (E2 Inc 4 4b) ----


def _card(title, why="why", scope="project", source="CLAUDE.md"):
    """Build one stored discipline dict (the shape the push carries)."""
    return {"title": title, "why": why, "scope": scope, "source": source}


def test_project_disciplines_none_until_pushed(tmp_path):
    """project_disciplines returns None for a project that never pushed any.

    Why this matters: None (no row) must be distinct from an empty card list (pushed,
    none) so the serializer can skip a never-pushed project rather than show a section.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert project_disciplines(conn, "demo") is None


def test_project_disciplines_round_trips_cards_and_updated_at(tmp_path):
    """An upserted discipline set is read back as the same cards plus its freshness stamp.

    Why this matters: the project page renders these cards verbatim and shows the push
    date, so both the JSON (title/why/scope/source) and updated_at must round-trip.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    cards = [_card("Local-first", scope="global", source="CLAUDE.md")]
    upsert_disciplines(conn, "demo", cards, "2026-06-27T10:00:00+00:00")
    assert project_disciplines(conn, "demo") == {
        "cards": cards,
        "updated_at": "2026-06-27T10:00:00+00:00",
    }


def test_upsert_disciplines_replaces_prior_set(tmp_path):
    """A second push REPLACES the project's disciplines (current state, not append).

    Why this matters: disciplines are current state like the checklist — re-extracting a
    revised doc must overwrite the prior cards, not accumulate them, and re-stamp updated_at.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_disciplines(conn, "demo", [_card("Old")], "2026-06-27T10:00:00+00:00")
    upsert_disciplines(conn, "demo", [_card("New")], "2026-06-27T11:00:00+00:00")
    assert project_disciplines(conn, "demo") == {
        "cards": [_card("New")],
        "updated_at": "2026-06-27T11:00:00+00:00",
    }


def test_upsert_empty_disciplines_clears_to_empty_list(tmp_path):
    """An empty push is stored as [] (cleared), distinct from None (never pushed).

    Why this matters: a doc that once stated principles but no longer does should clear
    the section, and [] vs None lets the serializer tell "cleared" from "never had any".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_disciplines(conn, "demo", [_card("X")], "2026-06-27T10:00:00+00:00")
    upsert_disciplines(conn, "demo", [], "2026-06-27T11:00:00+00:00")
    assert project_disciplines(conn, "demo")["cards"] == []


# --- relay_producer_disciplines: per-producer disciplines (C3 Inc 2.5, Unit 1.3) ----
# The four-test producer pattern (mirrors relay_producer_checklists): keyed coexistence +
# replace-in-place, revoked-producer exclusion, unknown-project empty, updated_at carried —
# plus hard-delete cleanup. This is the storage-now-display-later seam; nothing reads it yet.


def test_producer_disciplines_are_keyed_per_producer_and_replace_in_place(tmp_path):
    """Two producers' disciplines coexist; a producer's re-push replaces ONLY its own row.

    Why this matters: the per-producer store is keyed (project, author_id), so two contributors
    on the same project keep independent discipline sets, and a producer pushing again overwrites
    its own row without touching the other's — the same guarantee producer_checklists gives.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    b_id = add_user(conn, "Teammate B", "vb", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    c_id = add_user(conn, "Teammate C", "vc", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    upsert_producer_disciplines(
        conn, "demo", b_id, "Teammate B", [_card("B-one")], "2026-06-25T00:00:00+00:00"
    )
    upsert_producer_disciplines(
        conn, "demo", c_id, "Teammate C", [_card("C-one")], "2026-06-25T01:00:00+00:00"
    )
    # Producer B re-pushes a revised set — replaces its own row only.
    upsert_producer_disciplines(
        conn, "demo", b_id, "Teammate B", [_card("B-two")], "2026-06-25T02:00:00+00:00"
    )

    got = producer_disciplines_for(conn, "demo")
    assert [(p["author_id"], p["author_name"]) for p in got] == [
        (b_id, "Teammate B"),
        (c_id, "Teammate C"),
    ]  # ordered by name; both present
    by_id = {p["author_id"]: p for p in got}
    assert [d["title"] for d in by_id[b_id]["disciplines"]] == ["B-two"]  # B's row replaced
    assert [d["title"] for d in by_id[c_id]["disciplines"]] == ["C-one"]  # C's row untouched


def test_producer_disciplines_excludes_revoked_producers(tmp_path):
    """A revoked contributor's per-producer disciplines drop out (current state, not history).

    Why this matters: like the per-producer checklist, per-producer disciplines are CURRENT state
    — a revoked contributor is off the project, so their stale set must not surface. The INNER
    JOIN on active users enforces it. We push two, revoke one, and assert only the active remains.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    b_id = add_user(conn, "Teammate B", "vb", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    c_id = add_user(conn, "Teammate C", "vc", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    upsert_producer_disciplines(conn, "demo", b_id, "Teammate B", [_card("B")], "2026-06-25T00:00:00+00:00")
    upsert_producer_disciplines(conn, "demo", c_id, "Teammate C", [_card("C")], "2026-06-25T00:00:00+00:00")
    revoke_user(conn, c_id)  # C leaves the project

    got = producer_disciplines_for(conn, "demo")
    assert [p["author_name"] for p in got] == ["Teammate B"]  # revoked C's stale set is gone


def test_producer_disciplines_for_unknown_project_is_empty(tmp_path):
    """A project with no per-producer disciplines returns [] (legacy-only / never pushed)."""
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert producer_disciplines_for(conn, "never-seen") == []


def test_producer_disciplines_for_carries_updated_at(tmp_path):
    """Each per-producer disciplines row surfaces its push time (parity with the checklist row)."""
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    b_id = add_user(conn, "Teammate B", "vb", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    upsert_producer_disciplines(
        conn, "demo", b_id, "Teammate B", [_card("B")], "2026-06-25T09:30:00+00:00"
    )
    (row,) = producer_disciplines_for(conn, "demo")
    assert row["updated_at"] == "2026-06-25T09:30:00+00:00"


def test_delete_user_removes_per_producer_disciplines(tmp_path):
    """Hard-deleting a user removes their live per-producer disciplines (live state goes).

    Why this matters: delete_user frees the name and drops LIVE per-producer state so nothing
    stale lingers; per-producer disciplines are live state exactly like the checklist rows, so
    they must be cleaned up too (the read helper's INNER JOIN would hide an orphan, but the row
    should not survive a hard delete).
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    b_id = add_user(conn, "Teammate B", "vb", "contributor", ["demo"], "test", "2026-06-25T00:00:00+00:00")
    upsert_producer_disciplines(conn, "demo", b_id, "Teammate B", [_card("B")], "2026-06-25T00:00:00+00:00")
    delete_user(conn, b_id)

    # No row survives the hard delete (query the table directly — the user row is gone too, so
    # producer_disciplines_for's JOIN would return [] regardless; assert the row itself is gone).
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM relay_producer_disciplines WHERE author_id = ?", (b_id,)
    ).fetchone()["n"]
    assert remaining == 0
