# =============================================================================
# cli/relay_admin.py
# -----------------------------------------------------------------------------
# Responsible for: The admin-side provisioning CLI: every `relay-user` and `relay-project` verb,
#                  talking to a running relay's admin API with the SEPARATE admin token.
# Role in project: Account lifecycle, grants, credentials, passwords, roles, visibility, lifecycle —
#                  the operator's control plane for the multi-party dashboard.
# Assumptions: Never prints a secret it did not just mint (a new key is shown ONCE, or via
#              --key-only for scripts).
# =============================================================================
from __future__ import annotations


import sys
from pathlib import Path

from orion.config import (
    ConfigError,
    load_relay_config,
)
from orion.delivery import DeliveryError
from orion.delivery.relay import (
    create_user as relay_create_user,
    delete_user as relay_delete_user,
    grant_projects as relay_grant_projects,
    ungrant_projects as relay_ungrant_projects,
    list_users as relay_list_users,
    revoke_user as relay_revoke_user,
    add_user_key as relay_add_user_key,
    list_user_keys as relay_list_user_keys,
    rename_user as relay_rename_user,
    set_project_lifecycle as relay_set_project_lifecycle,
    set_project_visibility as relay_set_project_visibility,
    set_user_operator as relay_set_user_operator,
    revoke_user_key as relay_revoke_user_key,
    set_user_password as relay_set_user_password,
    set_user_role as relay_set_user_role,
    unlock_user as relay_unlock_user,
)
from orion.secrets import SecretsError, get_required, load_secrets


def _load_relay_admin(config_path: Path) -> tuple[str, str]:
    """Load the relay URL + admin token for a `relay-user` command.

    Args:
        config_path: Path to orion.toml (its sibling .env holds the admin token).

    Returns:
        A (relay_url, admin_token) pair: the relay's base URL (from [relay] url) and the
        admin Bearer token (read from .env via admin_token_env_var).

    Raises:
        ConfigError: when no relay is enabled, or [relay] has no admin_token_env_var.
        SecretsError: when the named admin-token env variable is unset.

    Why:
        The three relay-user commands share the same prerequisites — an enabled relay,
        a configured admin-token env var, and the secret itself — so resolving them lives
        in one place (DRY). The admin token is SEPARATE from the push credential (token_env_var):
        provisioning must not ride on the push credential. Reading the secret here, in the
        CLI, matches every other command; a missing one is named by get_required, never printed.
    """
    # Use the focused relay-only loader: provisioning needs the [relay] table but NOT a
    # local project list, so an admin-only operator (runs the relay, reports elsewhere)
    # isn't blocked by full load_config's "defines no projects" requirement.
    relay_cfg = load_relay_config(config_path)
    load_secrets(config_path)
    if not relay_cfg.enabled:
        raise ConfigError(
            f"no relay is enabled in {config_path}. Enable the [relay] table "
            "(url + token_env_var + admin_token_env_var) to manage relay users."
        )
    if not relay_cfg.admin_token_env_var:
        raise ConfigError(
            f"[relay] in {config_path} has no admin_token_env_var. Add it (the .env "
            "variable holding the relay admin token, e.g. "
            'admin_token_env_var = "ORION_RELAY_ADMIN_TOKEN") to manage relay users.'
        )
    admin_token = get_required(relay_cfg.admin_token_env_var)
    return relay_cfg.url, admin_token


# AU1-R P3: a sentinel meaning "the admin call failed and its message is already on stderr".
# A distinct object rather than None because several admin client calls legitimately return
# None (the ones whose success carries no payload — deactivate, unlock, rename), so None cannot
# also mean failure. Its ONLY correct use is `if result is _ADMIN_CALL_FAILED: return 1`.
_ADMIN_CALL_FAILED = object()


def _run_admin_command(config_path: Path, call) -> object:
    """Load the relay admin credentials, run one admin API call, and report any failure.

    Args:
        config_path: Path to orion.toml (its sibling .env holds the admin token).
        call: A callable taking (relay_url, admin_token) and performing exactly one admin
            API request. Written as a lambda at each call site so the `relay_*` client
            function is looked up at CALL time — which is what keeps the tests'
            `monkeypatch.setattr(cli, "relay_...", ...)` patch points working.

    Returns:
        Whatever `call` returned (often a dict, sometimes None for the verbs whose success
        carries no payload), or `_ADMIN_CALL_FAILED` when the credentials could not be
        loaded or the request failed — in which case "Error: ..." is ALREADY on stderr and
        the caller should return 1.

    Why:
        All fifteen `relay-user` / `relay-project` commands opened with the same six lines:
        load the admin credentials, make one call, and turn ConfigError / SecretsError /
        DeliveryError into one "Error: ..." line plus exit 1 (AU1's 15× finding). That
        ceremony is not where any of those commands differ, and copying it is how the
        sixteenth verb would have acquired a subtly different error path.

        The three exception types are caught TOGETHER on purpose: from the operator's seat a
        misconfigured relay, a missing secret and a rejected request are all "this did not
        happen, here is why", all fixable locally, and none of them a bug worth a traceback.

        Each command's own pre-flight validation (argument pairings, empty-list checks) stays
        in the command — that is per-command intent, not ceremony, and it must keep running
        BEFORE any credential is loaded.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        return call(relay_url, admin_token)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _ADMIN_CALL_FAILED


def _member_scope_sentence(scope: list[str]) -> str:
    """Describe what a `member` account can read, given its explicit grants.

    Args:
        scope: The member's explicitly granted project names (often empty).

    Returns:
        A sentence describing the account's effective read scope.

    Why:
        Every other scoped role is grant-only, so "no grants" means "sees nothing" and the
        CLI tells the operator to grant something. A member inverts that: it is an ORG
        INSIDER that reads every org-visible project WITHOUT a grant, and grants are
        ADDITIVE on top (visibility is a floor, not a ceiling). Reusing the grant-only
        wording for a member therefore states something false and prompts the operator to
        "fix" a configuration that is already correct — zero grants is the intended shape.
        Both the provisioning and role-change paths need to say this, identically, so the
        sentence lives here rather than being written twice (they drifted once already).
    """
    if scope:
        return f"every org-visible project, plus these grants: {', '.join(scope)}"
    return "every org-visible project (a member needs no grants)."


def cmd_relay_user_add(
    name: str,
    role: str,
    projects: list[str],
    config_path: Path,
    account_kind: str = "human",
    operated_by: str | None = None,
    key_only: bool = False,
) -> int:
    """Provision a relay user and print their one-time access key (`relay-user add`).

    Args:
        name: The new user's unique handle.
        role: "viewer" or "admin".
        projects: Project names a viewer may see (ignored for an admin).
        config_path: Path to orion.toml.
        account_kind: "human" (default) or "agent" (Unit 4a).
        operated_by: For an agent, the operating human's account name; None for a human.
        key_only: Scripting mode (CS-O PR3): print ONLY the raw key + one newline on
            stdout, nothing else — so `KEY=$(orion relay-user add ...)` works. Errors
            stay on stderr, and the key is only printed from a successful response
            (no partial output on failure).

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request
        (e.g. a duplicate name → the relay's 409, surfaced as a clear message).

    Why:
        The admin-facing half of provisioning: it calls the relay's POST /api/users with
        the admin token and prints the returned key ONCE. The key is shown here (to the
        operator who created it) deliberately — that is the only time it exists in the
        clear; it is never stored or logged. We print a copy-it-now warning so the operator
        knows it cannot be retrieved later (only its verifier is stored).
    """
    # Caught here rather than server-side alone so the operator gets an immediate, local
    # error instead of a round trip — the relay validates this too (never trust the client).
    is_agent = account_kind == "agent"
    if is_agent and not operated_by:
        print(
            "Error: --kind agent requires --operated-by NAME (the human it acts for).",
            file=sys.stderr,
        )
        return 1
    if not is_agent and operated_by:
        print("Error: --operated-by is only valid with --kind agent.", file=sys.stderr)
        return 1

    result = _run_admin_command(
        config_path,
        lambda url, token: relay_create_user(
            url,
            token,
            name,
            role,
            projects,
            account_kind="agent" if is_agent else None,
            operated_by=operated_by if is_agent else None,
        ),
    )
    if result is _ADMIN_CALL_FAILED:
        return 1

    if key_only:
        # The whole contract: raw key + one newline, nothing else on stdout.
        print(result["key"])
        return 0

    print(f"Provisioned user {result['name']!r} (role: {result['role']}).")
    if result.get("kind") == "agent":
        print(f"  Kind: agent, operated by {result['operated_by']!r}.")
    scope = result.get("projects") or []
    if result["role"] == "admin":
        print("  Scope: all projects (admin).")
    elif result["role"] == "member":
        print(f"  Scope: {_member_scope_sentence(scope)}")
    elif scope:
        print(f"  Scope: {', '.join(scope)}")
    elif result["role"] == "contributor":
        print("  Scope: none yet — grant projects so this contributor can push to them.")
    else:
        print("  Scope: none yet — grant projects so this viewer can see anything.")
    print()
    print("  Access key (shown ONCE — copy it now; it cannot be retrieved later):")
    print(f"    {result['key']}")
    return 0


def cmd_relay_user_list(config_path: Path) -> int:
    """List the relay's users with role, status, and scope (`relay-user list`).

    Args:
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success (including an empty roster); 1 on a config/secrets error
        or a failed request.

    Why:
        The operational view: who has access, what they can see, and who has been deactivated.
        It calls GET /api/users, which returns NO credential material (no verifier, no
        key), so a listing can never surface a secret.
    """
    result = _run_admin_command(config_path, lambda url, token: relay_list_users(url, token))
    if result is _ADMIN_CALL_FAILED:
        return 1

    users = result.get("users", [])
    if not users:
        print("No relay users provisioned yet.")
        return 0
    for user in users:
        status = "active" if user.get("active") else "DEACTIVATED"
        if user["role"] == "admin":
            scope = "all (admin)"
        else:
            scope = ", ".join(user.get("projects") or []) or "none"
        last_login = user.get("last_login_at") or "never"
        # Unit 4a: only an agent gets an extra marker, so a human roster prints exactly as
        # it did before. .get keeps this working against a pre-4a relay.
        if user.get("account_kind") == "agent":
            operator = user.get("operated_by_name") or "?"
            marker = f"  agent, operated by {operator}"
        else:
            marker = ""
        print(
            f"{user['name']}  [{user['role']}, {status}]  "
            f"scope: {scope}  last login: {last_login}{marker}"
        )
    return 0


def cmd_relay_user_deactivate(name: str, config_path: Path) -> int:
    """Deactivate a relay account: keys stop working + force-logout (`relay-user deactivate`).

    Args:
        name: The user to deactivate.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request
        (e.g. an unknown name → the relay's 404, surfaced as a clear message).

    Why:
        Immediate cutoff is a settled Increment-1 requirement. The verb renamed from
        `revoke` in CS-O PR3 (decision 9, clean break): account-level DEACTIVATE keeps
        the name, `delete` frees it, and `key revoke` kills one credential — three
        different acts that should not share a word. Semantics are untouched: it calls
        POST /api/users/revoke (the wire route keeps its legacy name — renaming it is a
        relay-side protocol change this CLI-scoped PR deliberately does not make), where
        the relay deactivates the user and bumps their session_version atomically — so
        keys stop authenticating AND any cookie already in a browser dies on its next
        request.
    """
    if _run_admin_command(
        config_path, lambda url, token: relay_revoke_user(url, token, name)
    ) is _ADMIN_CALL_FAILED:
        return 1

    print(
        f"Deactivated user {name!r}: their keys stop authenticating and any live "
        "session is logged out. The name is kept — `relay-user delete` frees it."
    )
    return 0


def cmd_relay_user_grant(name: str, projects: list[str], config_path: Path) -> int:
    """Grant an existing user access to more projects (`relay-user grant`).

    Args:
        name: The user whose scope to widen.
        projects: Project names to grant (at least one required).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on no --project given, a config/secrets error, or a failed
        request (e.g. an unknown name → the relay's 404).

    Why:
        A contributor's project set was frozen at creation, so a multi-project producer couldn't
        be covered without re-provisioning (KI-31). This widens scope in place. It prints the FULL
        scope the relay returns after the grant, so the operator sees the new coverage at a glance.
    """
    if not projects:
        print(
            "Error: grant needs at least one --project (e.g. --project my-app).",
            file=sys.stderr,
        )
        return 1
    result = _run_admin_command(
        config_path, lambda url, token: relay_grant_projects(url, token, name, projects)
    )
    if result is _ADMIN_CALL_FAILED:
        return 1

    scope = result.get("projects") or []
    print(f"Granted {name!r} access to: {', '.join(projects)}.")
    print(f"  Scope is now: {', '.join(scope) if scope else '(none)'}")
    return 0


def cmd_relay_user_ungrant(name: str, projects: list[str], config_path: Path) -> int:
    """Remove projects from an existing user's scope (`relay-user ungrant`).

    Args:
        name: The user whose scope to narrow.
        projects: Project names to ungrant (at least one required).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success (including a zero-removal no-op — ungrant is
        idempotent); 1 on no --project given, a config/secrets error, or a failed
        request (e.g. an unknown name → the relay's 404).

    Why:
        Grant's missing inverse (KI-40): scope is a security control, and one that only
        widens is the wrong shape. The output reports removed-vs-requested (names the
        user never held are skipped, not errors), the remaining scope, and — from the
        relay's `still_visible` field — which removed projects a `member` account can
        STILL read because they are org-visible. That note rides server facts: only the
        relay knows org visibility, so the CLI must not guess it.
    """
    if not projects:
        print(
            "Error: ungrant needs at least one --project (e.g. --project my-app).",
            file=sys.stderr,
        )
        return 1
    result = _run_admin_command(
        config_path, lambda url, token: relay_ungrant_projects(url, token, name, projects)
    )
    if result is _ADMIN_CALL_FAILED:
        return 1

    removed = result.get("removed") or []
    scope = result.get("projects") or []
    still_visible = result.get("still_visible") or []
    # The server normalized (strip/de-dupe) before matching, so diff against the same
    # normalization or a duplicated --project flag would show up as "not held".
    requested = list(dict.fromkeys(p.strip() for p in projects if p.strip()))
    not_held = [p for p in requested if p not in removed]
    if removed:
        print(f"Ungranted from {name!r}: {', '.join(removed)}.")
    else:
        print(f"Nothing removed from {name!r}.")
    if not_held:
        print(f"  Not held (nothing to remove): {', '.join(not_held)}")
    print(f"  Scope is now: {', '.join(scope) if scope else '(none)'}")
    if still_visible:
        print(
            f"  Note: {', '.join(still_visible)} remain(s) readable — org-visible, and "
            f"{name!r} is a member (org visibility is a floor grants stack on)."
        )
    return 0


def cmd_relay_user_key_add(
    name: str, label: str, config_path: Path, key_only: bool = False
) -> int:
    """Attach a new key credential to an account (`relay-user key add`).

    Args:
        name: The account to attach the key to.
        label: A short label for where the key will live (unique among active
            credentials). Defaults to the constant "key" at the parser (CS-O PR3) —
            deliberately neutral, not hostname-derived, so no machine name leaks into
            relay state; a second label-less add on the same account is the relay's 409.
        config_path: Path to orion.toml.
        key_only: Scripting mode (CS-O PR3): print ONLY the raw key + one newline on
            stdout, nothing else. Errors stay on stderr; the key is only printed from a
            successful response (no partial output on failure).

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown
        name → 404; deactivated account or duplicate active label → the relay's 409).

    Why:
        The replacement for the retired `rotate`, and the command that makes one identity
        across two machines real. Adding does NOT disturb the account's existing keys, so the
        safe replacement sequence is add → deploy → verify → revoke: the old key keeps
        working until the new one is confirmed, with no silent-401 window on a scheduled
        push and no way to strand yourself if this output is lost.
    """
    result = _run_admin_command(
        config_path, lambda url, token: relay_add_user_key(url, token, name, label)
    )
    if result is _ADMIN_CALL_FAILED:
        return 1

    if key_only:
        # The whole contract: raw key + one newline, nothing else on stdout.
        print(result["key"])
        return 0

    print(f"Added key {label!r} (id {result['id']}) to {name!r}. Existing keys still work.")
    print("  Access key (shown ONCE — copy it now; it cannot be retrieved later):")
    print(f"    {result['key']}")
    print("  Next: install it, verify a push, THEN revoke the old credential:")
    print(f"    orion relay-user key list {name}")
    return 0


def cmd_relay_user_key_list(name: str, config_path: Path) -> int:
    """List an account's credentials (`relay-user key list`).

    Args:
        name: The account whose credentials to list.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request.

    Why:
        The question that has to precede a revocation is "what is attached, and is it still
        being used?" — `last_used_at` answers the second half, which is otherwise unknowable
        once an account holds several keys. Key material is never shown: the relay's listing
        excludes verifiers by construction.
    """
    result = _run_admin_command(
        config_path, lambda url, token: relay_list_user_keys(url, token, name)
    )
    if result is _ADMIN_CALL_FAILED:
        return 1

    credentials = result.get("credentials", [])
    if not credentials:
        print(f"{name!r} has no credentials.")
        return 0
    print(f"Credentials for {name!r}:")
    for cred in credentials:
        state = "active" if cred["active"] else "revoked"
        used = cred["last_used_at"] or "never used"
        print(f"  [{cred['id']:>3}] {cred['type']:<8} {cred['label']:<12} {state:<8} last used: {used}")
    return 0


def cmd_relay_user_key_revoke(name: str, credential_id: int, config_path: Path) -> int:
    """Revoke one credential by id (`relay-user key revoke`).

    Args:
        name: The account the credential belongs to.
        credential_id: The credential id (from `key list`).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown
        account, or an id that is unknown / already revoked / not owned by this account → 404).

    Why:
        Revokes exactly one credential, leaving the account and its other keys intact — the
        whole point of the credential split (a lost laptop kills one key, not an identity).
        It deliberately does not log the person out of the dashboard: a machine key and a
        browser session are different credentials with different lifecycles.
    """
    if _run_admin_command(
        config_path,
        lambda url, token: relay_revoke_user_key(url, token, name, credential_id),
    ) is _ADMIN_CALL_FAILED:
        return 1

    print(f"Revoked credential {credential_id} for {name!r}. Their other credentials still work.")
    return 0


def cmd_relay_user_password_set(name: str, generate: bool, config_path: Path) -> int:
    """Set or replace an interactive account's password (`relay-user password set`).

    Args:
        name: The account to set a password for.
        generate: When True, the relay mints the password and prints it once instead of
            prompting for one.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request; 2 when the
        two typed passwords do not match.

    Why:
        The password is read with `getpass` (hidden, prompted twice) and is never accepted as
        a command-line argument — argv is visible in shell history, in `ps`, and in CI logs.
        `--generate` exists for provisioning someone else's account, where the admin should
        not be inventing (or seeing twice) a password the person will own.

        Note the consequence printed below: once an account has a password, its access KEYS
        stop working for login. That is the intended end state — humans know a password,
        machines hold a key — but it takes effect immediately, so the person must be told.
    """
    password = None
    if not generate:
        import getpass

        try:
            password = getpass.getpass(f"New password for {name!r}: ")
            confirm = getpass.getpass("Repeat password: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 2
        if not password:
            print("Error: password must not be empty.", file=sys.stderr)
            return 2
        if password != confirm:
            print("Error: the two passwords did not match.", file=sys.stderr)
            return 2

    result = _run_admin_command(
        config_path, lambda url, token: relay_set_user_password(url, token, name, password)
    )
    if result is _ADMIN_CALL_FAILED:
        return 1

    print(f"Password set for {name!r}. Any live session was logged out.")
    if "password" in result:
        print("  Password (shown ONCE — copy it now; it cannot be retrieved later):")
        print(f"    {result['password']}")
    print("  They now log in with their NAME and this password.")
    print("  Their access key no longer works for login (it stays valid for machine pushes).")
    return 0


def cmd_relay_user_password_unlock(name: str, config_path: Path) -> int:
    """Clear an account's login lockout (`relay-user password unlock`).

    Args:
        name: The account to unlock.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request.

    Why:
        Repeated failed logins lock an account for a short window, which means anyone who
        knows the account name can lock its owner out. Keeping the lockout strict is right —
        it is what makes online password guessing hopeless — so the counterweight is an
        instant admin unlock that does NOT require changing a password the person still knows.
    """
    if _run_admin_command(
        config_path, lambda url, token: relay_unlock_user(url, token, name)
    ) is _ADMIN_CALL_FAILED:
        return 1

    print(f"Cleared the login lockout for {name!r}. Their password is unchanged.")
    return 0


def cmd_relay_user_role(name: str, role: str, config_path: Path) -> int:
    """Change an account's role (`relay-user role`).

    Args:
        name: The account whose role to change.
        role: The new role (the relay validates it against the provisionable set).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request.

    Why:
        Roles were fixed at provisioning, so changing one previously meant delete + re-add —
        minting a new key the person must be re-issued and dropping their grants. The warning
        below is the operationally important part: demoting an admin to a scoped role makes
        default-deny apply, so the account sees NOTHING until it is granted projects.
    """
    result = _run_admin_command(
        config_path, lambda url, token: relay_set_user_role(url, token, name, role)
    )
    if result is _ADMIN_CALL_FAILED:
        return 1

    print(f"{name!r} is now a {role}. Any live session was logged out.")
    scope = result.get("projects") or []
    if role == "member":
        # NOT the default-deny warning below: a member reads every org-visible project
        # with no grant at all, so "no grants" is its intended shape. Warning here would
        # state something false — that the account sees nothing — and push the operator
        # to "fix" a correct configuration.
        print(f"  Scope: {_member_scope_sentence(scope)}")
    elif role != "admin" and not scope:
        print("  WARNING: this account has NO project grants, so it currently sees nothing.")
        print(f"    Grant projects with: orion relay-user grant {name} <project> [<project> ...]")
    elif role != "admin":
        print(f"  Scope: {', '.join(scope)}")
    return 0


def cmd_relay_project_visibility(name: str, visibility: str, config_path: Path) -> int:
    """Set a project's visibility (`relay-project visibility`).

    Args:
        name: The project to set.
        visibility: "org" (every member may read it) or "restricted" (grant-only).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (an unknown
        project → the relay's 404).

    Why:
        The KB's scoping act: 'org' is what lets a member-role account read a project without
        a per-project grant. Every project is born 'restricted', so opening one up is always
        deliberate — which is the property that keeps default-deny meaningful as the org's
        project list grows.
    """
    if _run_admin_command(
        config_path,
        lambda url, token: relay_set_project_visibility(url, token, name, visibility),
    ) is _ADMIN_CALL_FAILED:
        return 1

    if visibility == "org":
        print(f"{name!r} is now org-visible: any member-role account can read it.")
    else:
        print(f"{name!r} is now restricted: only accounts granted it can read it.")
    print("  Viewers and supervisors are unaffected — they always see only their grants.")
    return 0


def cmd_relay_project_lifecycle(name: str, lifecycle: str, config_path: Path) -> int:
    """Mark a project finished or still running (`relay-project lifecycle`).

    Args:
        name: The project to set.
        lifecycle: "past" (finished) or "active" (still running).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (an unknown
        project → the relay's 404).

    Why:
        The KB's curation act (S2.2): a project is finished because someone SAYS so, never
        because it went quiet — quiet is not finished, and inferring it would let a paused
        project read as shipped. The flag lives on the relay (not in orion.toml) so the
        dashboard keeps remembering after the project stops being produced, which is why
        this is an admin command rather than something a producer pushes.

        Nothing about the project's record changes — reports, checklist, About and
        discussions all stay exactly as they are. What changes is how the dashboard frames
        it, and that a finished project stops being read as if it still had deadlines.
    """
    if _run_admin_command(
        config_path,
        lambda url, token: relay_set_project_lifecycle(url, token, name, lifecycle),
    ) is _ADMIN_CALL_FAILED:
        return 1

    if lifecycle == "past":
        print(f"{name!r} is now marked past: finished, and framed as history.")
        print("  It groups into the dashboard's 'Past projects' section.")
        print(
            "  It is excluded from every deadline view — due-soon, at-risk, slipping and "
            "Scheduling — so it can never read as overdue."
        )
        print("  Its full record (reports, checklist, About, discussion) is unchanged.")
    else:
        print(f"{name!r} is active again: back in the live sections, deadlines and all.")
    return 0


def cmd_relay_user_set_operator(name: str, operator: str, config_path: Path) -> int:
    """Repoint an agent account at a different operating human (`relay-user set-operator`).

    Args:
        name: The agent account to reassign.
        operator: The human account the agent should act on behalf of.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown
        agent → 404; not an agent, or an invalid operator → the relay's 400).

    Why:
        The explicit escape hatch for the blocked operator delete: an account that still
        operates active agents cannot be deleted until they are reparented, and the
        alternative (delete + re-provision the agent) would mint a new key every machine
        holding it would need re-issued. Reassignment moves DISPLAY GROUPING only — stored
        reports keep the agent's real author id, so no history moves and provenance holds.
    """
    if _run_admin_command(
        config_path, lambda url, token: relay_set_user_operator(url, token, name, operator)
    ) is _ADMIN_CALL_FAILED:
        return 1

    print(f"{name!r} is now operated by {operator!r}.")
    print("  Past reports keep their own attribution; only display grouping moves.")
    return 0


def cmd_relay_user_rename(name: str, new_name: str, config_path: Path) -> int:
    """Rename an account (`relay-user rename`).

    Args:
        name: The account to rename.
        new_name: The new unique name.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown
        name → 404; taken name → the relay's 409).

    Why:
        The account is the durable identity under the credential split, so its label should be
        editable without re-provisioning. Already-recorded reports and discussion items keep
        the name they were written with — `author_name` is denormalized precisely so history
        survives the account changing or being deleted — so this is not a retroactive edit.
    """
    if _run_admin_command(
        config_path, lambda url, token: relay_rename_user(url, token, name, new_name)
    ) is _ADMIN_CALL_FAILED:
        return 1

    print(f"Renamed {name!r} to {new_name!r}. Past reports keep the name they were sent under.")
    return 0


def cmd_relay_user_delete(name: str, config_path: Path) -> int:
    """Hard-delete a user, freeing their name to be reused (`relay-user delete`).

    Args:
        name: The user to delete.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown name
        → 404).

    Why:
        `deactivate` keeps the row, so the UNIQUE name stays occupied and can't be re-provisioned
        (KI-31). `delete` removes the user + grants + live per-producer checklists and frees the
        name, while their past reports/replies keep the author name already recorded on them.
    """
    if _run_admin_command(
        config_path, lambda url, token: relay_delete_user(url, token, name)
    ) is _ADMIN_CALL_FAILED:
        return 1

    print(
        f"Deleted user {name!r}: the name is free to reuse. Their past reports and "
        "discussion replies keep the author name already recorded on them."
    )
    return 0
