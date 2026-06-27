// =============================================================================
// web/src/components/Shell.tsx
// -----------------------------------------------------------------------------
// Responsible for: The desktop shell — the sidebar + the content area where one
//                  screen renders (an <Outlet>). Loads the portfolio once (the
//                  sidebar's project/tracker lists + the home both read it) and
//                  shares it with child routes via the Outlet context.
// Role in project: The frame every dashboard screen renders inside. The login and
//                  showcase screens are full-bleed and live OUTSIDE this shell.
// Auth gate: when the relay is gated and there is no session, the shell redirects to
//            /login — so every nested route is protected in one place.
// =============================================================================

import { useEffect, useState } from "react";
import { Navigate, Outlet, useNavigate } from "react-router-dom";
import type { Me, Portfolio } from "../api/types";
import { getPortfolio, logout } from "../api/client";
import { Sidebar } from "./Sidebar";

/** What child routes can read from the shell via useOutletContext(). */
export interface ShellContext {
  me: Me;
  portfolio: Portfolio | null;
}

interface ShellProps {
  me: Me;
  /** Called after logout so the app re-reads /api/me (back to the anonymous state). */
  onAuthChange: () => void | Promise<void>;
}

export function Shell({ me, onAuthChange }: ShellProps) {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const navigate = useNavigate();
  // A gated relay with no valid session sends the viewer to /login. Computed before the
  // early return, but hooks above stay unconditional (rules-of-hooks safe).
  const gatedOut = me.gated && !me.authenticated;

  useEffect(() => {
    if (gatedOut) return; // nothing to load until authenticated
    let alive = true;
    getPortfolio()
      .then((p) => alive && setPortfolio(p))
      .catch(() => alive && setPortfolio(null));
    return () => {
      alive = false;
    };
  }, [gatedOut]);

  if (gatedOut) {
    return <Navigate to="/login" replace />;
  }

  const handleLogout = async () => {
    await logout().catch(() => {
      /* even if the call fails, drop our view of the session and go to login */
    });
    await onAuthChange();
    navigate("/login");
  };

  const context: ShellContext = { me, portfolio };

  return (
    <div className="shell">
      <Sidebar me={me} portfolio={portfolio} onLogout={handleLogout} />
      <main className="content">
        <Outlet context={context} />
      </main>
    </div>
  );
}
