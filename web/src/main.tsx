// =============================================================================
// web/src/main.tsx
// -----------------------------------------------------------------------------
// Responsible for: The SPA entry point. Mounts <App> under the providers (theme +
//                  router) and loads the self-hosted fonts + global styles.
// Role in project: The root wiring. Fonts are imported from @fontsource (self-hosted,
//                  bundled by Vite) so the production CSP can stay font-src 'self' — no
//                  Google Fonts CDN.
// =============================================================================

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

// Self-hosted fonts (the three design families, the weights the design uses).
import "@fontsource/hanken-grotesk/400.css";
import "@fontsource/hanken-grotesk/500.css";
import "@fontsource/hanken-grotesk/600.css";
import "@fontsource/hanken-grotesk/700.css";
import "@fontsource/newsreader/400.css";
import "@fontsource/newsreader/500.css";
import "@fontsource/newsreader/600.css";
import "@fontsource/spline-sans-mono/400.css";
import "@fontsource/spline-sans-mono/500.css";
import "@fontsource/spline-sans-mono/600.css";

import "./theme/tokens.css";
import "./styles/base.css";

import { App } from "./App";
import { ThemeProvider } from "./theme/ThemeProvider";

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("Root element #root not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <ThemeProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ThemeProvider>
  </StrictMode>,
);
