<!-- =========================================================================
docs/dashboard-hardening-kickoff.md
---------------------------------------------------------------------------
Responsible for: The execution plan for the dashboard security-hardening slice
                 that follows C3 Increment 1 — adding a Content-Security-Policy
                 and standard security response headers to the now-live relay
                 dashboard.
Role in project: A kickoff doc (like the archived phase-c2-kickoff). Read it at
                 the start of the hardening session, then execute. The access
                 model lives in docs/dashboard-auth.md, deploy steps in
                 docs/deployment.md, and the roadmap in plans/orion-plan.md.
========================================================================= -->

# Kickoff: dashboard security hardening (CSP + headers)

## Why

C3 Increment 1 put a multi-user, comment-bearing, login-gated dashboard on the public
internet (`orion-relay-horizon-c.fly.dev`). The authentication layer is solid, but the
dashboard sends no Content-Security-Policy and no standard security response headers. That
is defense-in-depth worth having now that the surface is internet-facing and renders
user-influenced text (comments, project names). This slice adds it.

It is deliberately hardening-only. No user-facing behavior changes. The KI-8 vestigial-schema
cleanup and the guest/demo view (C3 Increment 3) are out of scope. The guest view waits for
the Horizon-E "dashboard in full" work.

This is a `relay/`-local slice (`relay/render.py`, `relay/server.py`, and the relay tests). It
does not touch the local CLI or config, so it carries no coupling risk.

## Scope

The roadmap sync that used to be Task 1 is already done (committed `e740c6b`). Start at the CSP.

### 1. Content-Security-Policy (hash-based)

Every page is built by `_page()` in `relay/render.py`, which emits exactly two static inline
blocks: `<style>{_PAGE_CSS}</style>` and `<script>{_PAGE_JS}</script>`. There are no inline
`style=` attributes, no event handlers, and nothing external is loaded (system fonts, no CDN,
no `url()` or `@import`). So a hash-based CSP is the clean fit, and it cannot break the page as
long as the hash derives from the same constant the page renders.

- In `relay/render.py`: compute, at import, the base64 SHA-256 of `_PAGE_CSS` and `_PAGE_JS`
  (the exact bytes between the tags, confirmed to have no inner whitespace) and expose them
  formatted as `'sha256-...'`. Deriving them from the constants means the policy and the markup
  can never drift. If the CSS or JS changes, the hash changes with it.
- In `relay/server.py`: build the CSP from those hashes and send it on dashboard responses:
  `default-src 'self'; style-src 'self' '<css-hash>'; script-src 'self' '<script-hash>';
  img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'`.
  The hash allowances are inert on the `_simple_html` error pages (which carry no inline blocks),
  so one policy covers every HTML response.

### 2. Standard security headers

Add a small DRY helper on `_RelayHandler` and apply it in the three response writers
(`_send_html`, `_send_json`, `_send_redirect`), reusing the existing `extra_headers` merge
rather than duplicating the header loop.

- On all responses: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and
  `Strict-Transport-Security: max-age=63072000; includeSubDomains` only when HTTPS-exposed. Gate
  HSTS on the existing `self.server.secure_cookie` flag (the same hosted signal the cookie's
  `Secure` attribute uses), so a plain-HTTP loopback dev relay does not send it.
- On HTML responses only: the CSP from step 1 plus `X-Frame-Options: DENY` (legacy clickjacking
  cover alongside `frame-ancestors 'none'`).

### 3. Tests (`tests/test_relay_server.py`, `tests/test_relay_render.py`)

- Header presence against a running relay: a dashboard GET carries the CSP (with both hashes),
  `nosniff`, `Referrer-Policy`, and `X-Frame-Options`. The `_get` helper returns only
  `(code, text)`, so read headers with the manual-request pattern already used by
  `test_dashboard_no_session_redirects_and_login_page_hides_secret`, or extend a small helper.
- HSTS only when hosted: mirror `test_secure_cookie_only_when_hosted`. A relay built with
  `AuthConfig(..., secure_cookie=True)` sends HSTS; the default loopback relay does not.
- The contract test (pure render unit): render a page, extract the bytes inside
  `<style>...</style>` and `<script>...</script>`, compute their SHA-256, and assert each equals
  the hash `render.py` exposes. This guards the invariant that the CSP can never block the
  dashboard's own CSS/JS. It always passes (both derive from the same constant), but it documents
  and pins the contract.
- No regression: the existing dashboard, login, and comment tests stay green.

### 4. Docs

- `docs/known-issues.md` KI-19: mark Resolved, dated, noting the CSP plus security headers.
- One line in `docs/dashboard-auth.md` (the security-invariants section) noting the dashboard
  now sends a CSP and HSTS / nosniff / frame headers.
- These are security-adjacent docs, so they ride in the hardening PR rather than a direct commit.

## Verification

- `PYTHONPATH=src python -m pytest -q` green. CI is capped until 2026-07-01, so local-green is the
  gate.
- Eyes-on the live relay, which is the real test for a CSP. A policy that blocks the page is worse
  than none, so this is mandatory, not optional:
  - `curl -I` the dashboard and confirm the CSP and headers are present.
  - Open `orion-relay-horizon-c.fly.dev` in a browser, log in, and confirm in DevTools that there
    are no CSP violations in the console and that the styling and the relative-time JS still work.
- Ship as one PR (relay code is PR-gated). Merge on local-green review while CI is capped.

## Out of scope (named, deferred)

KI-8 vestigial-schema cleanup, the by-design-only KIs (KI-5 and similar), C3 Increment 2 (write)
and 3 (guest/demo), and Horizon E scoping. The "what feature comes next" direction is its own
planning moment after this pass. Horizon E is the recorded next band but is marked unvalidated,
so it needs a scoping and validation pass before any build, not a blind commitment.
