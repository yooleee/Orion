// =============================================================================
// web/vite.config.ts
// -----------------------------------------------------------------------------
// Responsible for: The Vite build + dev-server config for the Orion dashboard SPA,
//                  plus the Vitest (unit/component test) config in the same file.
// Role in project: Builds to web/dist (served single-host by the relay in prod) and,
//                  in dev, proxies the API/auth paths to the local relay so the
//                  browser sees one origin and the session cookie flows.
// Notes: the relay (default 127.0.0.1:8787) must be running for `npm run dev` to
//        reach live data. Production serves the built assets from the relay itself
//        (slice 4a.5), so no proxy is involved there.
// =============================================================================

/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The local relay the dev server proxies to. Kept here so all proxied paths share it.
const RELAY = "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    // Don't inline assets as data: URIs — keep every asset an external file so the
    // production CSP can stay `img-src 'self'` without a `data:` allowance (4a.5).
    assetsInlineLimit: 0,
  },
  server: {
    port: 5173,
    // Forward ONLY the JSON API to the relay (the session cookie flows through, so the
    // browser sees same-origin). Everything else — including the client-side /login route —
    // is served by Vite's index.html fallback so the SPA router owns it. The SPA never uses
    // the relay's legacy form GET/POST /login; it authenticates via POST /api/login.
    proxy: {
      "/api": RELAY,
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: true, // let components import .css without the test runner choking
  },
});
