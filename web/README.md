<!-- =========================================================================
web/README.md
---------------------------------------------------------------------------
Responsible for: How to develop, test, and build the Orion dashboard SPA.
Role in project: The E2 Inc 4 frontend (React/Vite/TS) that consumes the relay's
                 read-only JSON API. Build plan + decisions:
                 ../docs/e2-inc4-dashboard-rebuild-kickoff.md. API contract:
                 ../docs/dashboard-api-contract.md. Visual spec: ../design/.
========================================================================= -->

# Orion dashboard (web)

A React + Vite + TypeScript single-page app. It renders the dashboard from the relay's
read-only JSON API and is themeable across Dark / Sepia (default) / Light. In production the
relay serves the built assets single-host (slice 4a.5); in development Vite serves the SPA and
proxies `/api` to a locally-running relay.

## Develop

Two processes. Start the relay first, then the dev server.

1. **Run the relay** on `127.0.0.1:8787` (from the repo root):

   ```
   PYTHONPATH=src python -m orion.cli relay-serve \
     --host 127.0.0.1 --port 8787 \
     --db /path/to/relay.sqlite3 --token-env ORION_RELAY_TOKEN
   ```

   The dev server proxies `/api` to this address (see `vite.config.ts`), so the browser
   treats the API as same-origin and the session cookie flows. The SPA's client-side routes
   (`/login`, `/project/...`, etc.) are served by Vite, not proxied.

   **CSRF in dev:** `POST /api/login` / `/api/logout` enforce a same-origin check. Because the
   browser's origin in dev is the Vite server (`http://localhost:5173`) while the relay is on
   `:8787`, point the relay's public origin at the dev SPA origin
   (`ORION_RELAY_PUBLIC_ORIGIN=http://localhost:5173`) so the check passes. In production the
   SPA and API share one origin, so this is set to that origin instead.

2. **Run the dev server:** `npm run dev` → http://localhost:5173

## Test

```
npm test           # Vitest, run once
npm run test:watch # watch mode
```

Two guarantee-tests lock in the invariants that carry over from the server-rendered
dashboard:

- **State legible without colour alone** (`StatusSignal.test.tsx`): every status renders a
  glyph **and** a text label, never colour alone.
- **Untrusted text renders inert** (`Sidebar.test.tsx`): stored, attacker-influenceable text
  (project/tracker names) renders as inert text, never as live DOM (no
  `dangerouslySetInnerHTML` for stored content).

## Build

```
npm run build      # tsc type-check + vite build → dist/
```

`dist/` (gitignored) is what the relay serves in production. Fonts (Hanken Grotesk,
Newsreader, Spline Sans Mono) are self-hosted via `@fontsource/*` and bundled into `dist/`, so
the production CSP can stay `font-src 'self'` with no third-party CDN.

## Layout

- `src/api/` — the typed API client + the wire types (mirrors `../docs/dashboard-api-contract.md`).
- `src/theme/` — `tokens.css` (the three theme token sets), `ThemeProvider`, and `status.ts`
  (the status → glyph/label/colour table; the client half of the design vocabulary).
- `src/components/` — the shell (`Shell`, `Sidebar`), `ThemeSwitcher`, and `StatusSignal`.
- `src/routes/` — one component per screen. Home / Project / Report are stubs in slice 4a.2 and
  are built out in 4a.3 / 4a.4.
