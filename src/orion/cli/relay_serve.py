# =============================================================================
# cli/relay_serve.py
# -----------------------------------------------------------------------------
# Responsible for: The `relay-serve` launcher: resolves settings (flag > [relay.serve] > default),
#                  reads the relay's secrets, bootstraps missing ones (--init-secrets), and
#                  hands off to relay/server.py.
# Role in project: The thin adapter over the separately-deployable relay package. The relay is
#                  imported LAZILY (_load_relay_serve) so every other command works without it.
# Assumptions: ORION_RELAY_USER_PEPPER is required unconditionally (contributor keys are the only
#              push credential).
# =============================================================================
from __future__ import annotations


import argparse
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orion.config import (
    ConfigError,
    RELAY_SERVE_KEYS,
    RelayServeSettings,
    load_relay_serve_settings,
    parse_showcase_projects,
    resolve_relay_serve_settings,
)
from orion.secrets import SecretsError, bootstrap_env_secrets, load_secrets


def _load_relay_serve():
    """Import the relay server's serve() from the top-level relay/ package.

    Returns:
        The relay.server.serve callable.

    Why:
        The relay (the hosted half) lives in a top-level `relay/` package at the
        repo root, deliberately OUTSIDE the installed `orion` package (src/), so the
        core stays dependency-light and the relay is separately deployable.
        `relay-serve` is a convenience launcher for that bundled reference relay,
        which only exists when Orion is run from a clone of its repo. We import it
        LAZILY here (not at module top) so importing cli.py — i.e. every other
        command — never depends on the relay being present. A console-script entry
        point does not put the repo root on sys.path, so on the first ImportError we
        add it (relay/ sits at parents[2] of this file: src/orion/cli.py -> repo
        root) and retry; a remaining failure becomes a clear, actionable message
        rather than a raw ImportError.
    """
    try:
        from relay.server import serve

        return serve
    except ImportError:
        pass

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from relay.server import serve

        return serve
    except ImportError as exc:
        raise ConfigError(
            "Could not import the relay package. `relay-serve` runs the bundled "
            "reference relay, which is only available when running Orion from a "
            "clone of its repository (the relay/ package is not part of the "
            "installed orion distribution)."
        ) from exc


def _relay_serve_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Collect the relay-serve settings the command line ACTUALLY supplied.

    Args:
        args: The parsed `relay-serve` namespace. Every setting flag defaults to
            argparse.SUPPRESS, so an omitted flag is simply absent from the namespace.

    Returns:
        A dict keyed by RELAY_SERVE_KEYS names holding only the supplied values, already
        in the types RelayServeSettings carries (Path for db / web_dir, the parsed
        (name, blurb) pairs for showcase_projects). Empty when no setting flag was given.

    Why:
        This is the "flag" layer of the flag > config > default precedence (CS-O PR8),
        kept as an explicit key list (not `vars(args)`) so non-setting attributes such as
        `config`, `command` and `allow_legacy_admin` can never leak into the resolver. A
        relative --db / --web-dir stays relative to the working directory here, exactly as
        the flags always behaved; only [relay.serve] paths resolve beside the config.
    """
    overrides: dict[str, object] = {}
    for key in RELAY_SERVE_KEYS:
        if not hasattr(args, key):
            continue  # omitted on the command line -> the config/default layer decides
        value = getattr(args, key)
        if key in ("db", "web_dir"):
            value = Path(value)
        elif key == "showcase_projects":
            value = parse_showcase_projects(value)
        overrides[key] = value
    return overrides


def _init_relay_secrets(env_path: Path, settings: RelayServeSettings) -> int:
    """Generate the relay's missing secrets into `env_path` and report by name (CS-O PR9).

    Args:
        env_path: The .env to update (beside the config the caller was given).
        settings: The resolved relay-serve settings; they decide whether the view token
            is part of the required set (see Why).

    Returns:
        Exit code 0 after reporting (also when nothing was missing); 1 if the file could
        not be written (the OSError message is printed, never a value).

    Why:
        The three fixed-name secrets are needed by EVERY relay (the pepper unconditionally,
        the session key and admin token by every gated or provisioning relay). The VIEW
        token is the decided exception: it is generated only when these settings would
        actually need it — a non-loopback bind or --require-view-auth, the same predicate
        the bind guard applies — because generating it for a plain loopback dev relay
        would silently GATE that dashboard (the bootstrap-admin login kicks in the moment
        the token exists), a behavior change nobody asked for; while skipping it silently
        on a hosted relay would leave the secret set incomplete. So it is generated when
        needed and otherwise SKIPPED WITH THE REASON printed. Output names variables and
        outcomes only — a generated value is never echoed, which is why `fly secrets
        import < .env` (not copy-paste from a terminal) is the documented hosted path.
    """
    # The predicate lives in the relay package (outside the installed distribution);
    # _load_relay_serve puts the repo root on sys.path (or raises the clear "no relay
    # package" ConfigError), exactly as the serving path does.
    _load_relay_serve()
    from relay.server import _is_loopback

    names = ["ORION_RELAY_USER_PEPPER", "ORION_RELAY_SESSION_KEY", "ORION_RELAY_ADMIN_TOKEN"]
    view_needed = settings.require_view_auth or not _is_loopback(settings.host)
    if view_needed:
        names.append(settings.view_token_env)
    try:
        report = bootstrap_env_secrets(env_path, names)
    except OSError as exc:
        print(f"Error: could not write {env_path}: {exc}", file=sys.stderr)
        return 1

    print(f"Relay secrets in {env_path}:")
    for name in names:
        if name in report.generated:
            print(f"  generated    {name}")
        elif name in report.filled:
            print(f"  filled       {name}  (was empty)")
        elif name in report.placeholders:
            print(
                f"  already set  {name}  ⚠ looks like a .env.example placeholder — "
                "replace it with a real secret (values are never overwritten)"
            )
        else:
            print(f"  already set  {name}")
    if not view_needed:
        print(
            f"  skipped      {settings.view_token_env}  (loopback relay: the dashboard stays "
            "open; re-run with --host <non-loopback> or --require-view-auth to generate it, "
            "or set it yourself)"
        )
    if not report.generated and not report.filled:
        print("Nothing to do: every required secret was already set.")
    print(
        "Hosted relay? A local .env does not populate Fly — run this against a relay-only "
        "directory and `fly secrets import < .env` (see docs/deployment.md)."
    )
    return 0


def cmd_relay_serve(
    overrides: dict[str, object],
    config_path: Path,
    allow_legacy_admin: bool = False,
    init_secrets: bool = False,
) -> int:
    """Run the local reference relay: ingest endpoint + read-only dashboard.

    Args:
        overrides: The relay-serve settings the command line actually supplied (see
            _relay_serve_overrides), applied over [relay.serve] over the defaults.
        config_path: Path to orion.toml. Locates the sibling .env that holds the secrets
            AND the optional [relay.serve] settings table (CS-O PR8). A missing file is
            not an error — the relay then runs on flags + defaults (the Fly image has no
            config file at all).
        allow_legacy_admin: Keep the shared view key usable as an admin login after users
            exist. Flag-only by design: deliberately not part of the settings layer.
        init_secrets: `--init-secrets` (CS-O PR9): generate the missing relay secrets into
            the sibling .env, report by NAME, and return WITHOUT serving. Runs before the
            pepper requirement below (the pepper may be exactly what it generates).

    Returns:
        Exit code: 0 on a clean shutdown (Ctrl-C) or after --init-secrets; 1 on a setup
        error (an invalid [relay.serve] table, a missing user pepper, an invalid timezone
        or web dir, the relay package can't be imported, the fail-closed guard refuses a
        non-loopback bind without a view secret, or the .env could not be written).

    Why:
        This is the thin CLI adapter over relay/server.py — it resolves the settings
        (flag > config > default, one pure function in config.py), reads the relay's
        secrets from .env and hands off to serve(). There is NO shared ingest secret to
        read: since CS-O PR7 every push authenticates with a per-producer contributor key
        held in the relay's database, and the ONE secret that makes those keys resolvable
        is ORION_RELAY_USER_PEPPER — so it is REQUIRED here unconditionally. A pepper-less
        relay would start fine and then 401 every push (a silently failing cron, not a
        human at a login form), which is exactly the failure mode a startup refusal with
        a named error prevents. The view secret is read softly (empty -> None) because on
        loopback the dashboard may serve open; the relay's guard — not this CLI — is
        what refuses a non-loopback bind without it, so the rule is enforced for every
        caller, not just this one. The display timezone is validated HERE for the flag
        path (config values were already validated at load) by constructing a ZoneInfo —
        the same check the renderer does, so "valid here" means "usable there". serve()
        blocks until interrupted, then returns.
    """
    try:
        # Load .env beside the config (like every command), then resolve the settings:
        # the [relay.serve] layer (defaults when the file or table is absent) with the
        # typed flags applied on top.
        load_secrets(config_path)
        settings = resolve_relay_serve_settings(
            load_relay_serve_settings(config_path), overrides
        )
        if init_secrets:
            return _init_relay_secrets(config_path.parent / ".env", settings)
        # Optional: empty/unset -> None. The fail-closed guard inside serve() refuses a
        # non-loopback bind when it is None; on loopback, None means "reads open".
        view_token = os.environ.get(settings.view_token_env, "").strip() or None

        # Multi-party auth secrets (fixed env names — secrets never live in orion.toml).
        # Each is INDEPENDENT of the view secret (Codex hardening): the session signing
        # key, the per-user-key pepper, and the provisioning admin token. The pepper is
        # REQUIRED: contributor keys are the only push credential, and without the pepper
        # none of them can be verified — a relay that could never accept a push. Fail
        # closed with a named error rather than 401 every cron push silently.
        session_key_raw = os.environ.get("ORION_RELAY_SESSION_KEY", "").strip()
        user_pepper_raw = os.environ.get("ORION_RELAY_USER_PEPPER", "").strip()
        if not user_pepper_raw:
            raise ConfigError(
                "ORION_RELAY_USER_PEPPER is not set in .env. The relay authenticates every "
                "push with a per-producer contributor key, and the pepper is what makes those "
                "keys verifiable — without it no push can ever succeed. Set it to a long random "
                "secret (independent of the other relay secrets) before serving."
            )
        admin_token = os.environ.get("ORION_RELAY_ADMIN_TOKEN", "").strip() or None
        public_origin = os.environ.get("ORION_RELAY_PUBLIC_ORIGIN", "").strip() or None

        # Validate the display zone by constructing it (ZoneInfo is internally cached,
        # so the same object is reused by the renderer). This mirrors config.py's
        # _parse_display_timezone: ZoneInfoNotFoundError = no such zone in the tz
        # database; ValueError = a malformed key (e.g. an absolute path). Both are a
        # user typo, so we surface a clear, named error instead of a raw traceback.
        try:
            display_tz = ZoneInfo(settings.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"timezone {settings.timezone!r} is not a valid IANA zone name ({exc}). "
                'Use a name like "America/Los_Angeles" or "UTC".'
            ) from exc

        # When asked to serve the SPA, fail fast on a missing build rather than 404-ing
        # every page at request time — a clear, actionable startup error.
        web_dir = settings.web_dir
        if web_dir is not None and not (web_dir / "index.html").is_file():
            raise ConfigError(
                f"web_dir {web_dir} has no index.html — build the SPA first "
                "(cd web && npm run build), or leave web_dir unset to run API-only."
            )

        serve = _load_relay_serve()
        # AuthConfig + ShowcaseConfig + the loopback test live in the relay package,
        # importable now that _load_relay_serve has put the repo root on sys.path.
        from relay.server import AuthConfig, ShowcaseConfig, _is_loopback

        # Sessions need their signing key whenever the dashboard is access-gated (a view
        # secret is set) or provisioning is enabled (an admin token is set). Fail CLOSED
        # with a named error rather than serving a login that could never work. (The
        # pepper is already guaranteed above, unconditionally.)
        if (view_token is not None or admin_token is not None) and not session_key_raw:
            raise ConfigError(
                "multi-party auth needs ORION_RELAY_SESSION_KEY in .env (a long random "
                "secret, independent of the view token and the pepper). Set it before "
                "serving an access-gated dashboard."
            )

        # Secure cookies whenever the relay is HTTPS-exposed: a non-loopback bind, or a
        # loopback bind behind a TLS proxy (--require-view-auth). Plain loopback http
        # dev stays non-Secure so the cookie still works there.
        auth = AuthConfig(
            session_key=session_key_raw.encode("utf-8") if session_key_raw else None,
            user_pepper=user_pepper_raw.encode("utf-8"),
            admin_token=admin_token,
            secure_cookie=settings.require_view_auth or not _is_loopback(settings.host),
            session_seconds=settings.session_days * 24 * 3600,
            public_origin=public_origin,
            allow_legacy_admin=allow_legacy_admin,
        )

        # The public Showcase allowlist + curated blurbs come from the flags or from
        # [relay.serve] (project names are not secrets). Disabled by default — a no-op
        # ShowcaseConfig() — so the public surface only exists when asked for.
        showcase = ShowcaseConfig(
            enabled=settings.showcase,
            projects=settings.showcase_projects,
        )
    except (SecretsError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        # Blocks until Ctrl-C; serve() prints its bound address and read-auth state.
        # The guard raises ValueError BEFORE binding if host is non-loopback without a
        # view secret — surfaced here as a clean, actionable error, not a traceback.
        serve(
            settings.host, settings.port, settings.db, view_token,
            settings.require_view_auth, display_tz,
            auth=auth, web_dir=web_dir, showcase=showcase,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
