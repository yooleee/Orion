// =============================================================================
// web/src/App.tsx
// -----------------------------------------------------------------------------
// Responsible for: App boot + the route table. Loads /api/me once on mount (the SPA's
//                  source of truth for gated/identity/scope), then renders the routes:
//                  the full-bleed Login, and everything else inside the Shell.
// Role in project: The top of the SPA. /api/me drives the full / scoped / empty shell
//                  and whether the shell redirects to login. Data fetching is a plain
//                  effect (the lightest thing that works) — no data library yet.
// =============================================================================

import { useCallback, useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import type { Me } from "./api/types";
import { getMe } from "./api/client";
import { Shell } from "./components/Shell";
import { Home } from "./routes/Home";
import { Login } from "./routes/Login";
import { Project } from "./routes/Project";
import { Report } from "./routes/Report";
import { Tracker } from "./routes/Tracker";
import { Scheduling } from "./routes/Scheduling";
import { Showcase } from "./routes/Showcase";
import { ShowcaseDemo } from "./routes/ShowcaseDemo";
import { NotFound, Todos } from "./routes/placeholders";

export function App() {
  // null = still loading the initial /api/me; undefined would conflate with "anonymous".
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadMe = useCallback(async () => {
    try {
      setMe(await getMe());
      setError(null);
    } catch {
      setError("Could not reach Orion. Is the relay running?");
    }
  }, []);

  useEffect(() => {
    loadMe();
  }, [loadMe]);

  if (error) {
    return <div className="center-note">{error}</div>;
  }
  if (me === null) {
    return <div className="center-note">Loading…</div>;
  }

  return (
    <Routes>
      <Route path="/login" element={<Login me={me} onAuthChange={loadMe} />} />
      {/* Public, no-login — full-bleed, OUTSIDE the Shell (its own top bar, no sidebar). */}
      <Route path="/showcase" element={<Showcase />} />
      {/* The fabricated sample-project walkthrough reached from the Showcase landing. */}
      <Route path="/showcase/demo" element={<ShowcaseDemo />} />
      <Route element={<Shell me={me} onAuthChange={loadMe} />}>
        <Route index element={<Home />} />
        <Route path="project/:name" element={<Project />} />
        <Route path="report/:id" element={<Report />} />
        <Route path="todos" element={<Todos />} />
        <Route path="tracker/:name" element={<Tracker />} />
        <Route path="scheduling" element={<Scheduling />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
