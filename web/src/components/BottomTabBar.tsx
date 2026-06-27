// =============================================================================
// web/src/components/BottomTabBar.tsx
// -----------------------------------------------------------------------------
// Responsible for: The mobile primary nav — a fixed bottom tab bar (Projects, To-dos,
//                  Schedule, More) that replaces the desktop sidebar on narrow screens,
//                  plus a "More" sheet for the secondary actions (theme, showcase,
//                  account/logout) the sidebar's bottom block holds.
// Role in project: The mobile half of the shell's navigation. Rendered alongside the
//                  Sidebar in Shell and shown/hidden purely by CSS at the breakpoint —
//                  no JS resize listener. Active-tab state comes from navActive(), the
//                  SAME source the sidebar uses, so the two navs can't disagree.
// Accessibility: each tab is glyph + text label (legible without colour); tap targets
//                are ≥44px (set in CSS).
// =============================================================================

import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import type { Me } from "../api/types";
import { navActive } from "../lib/navState";
import { ThemeSwitcher } from "./ThemeSwitcher";

interface BottomTabBarProps {
  me: Me;
  onLogout: () => void;
}

/** One primary tab: a route link with a glyph + label, active when its section is. */
function Tab({ to, glyph, label, active }: { to: string; glyph: string; label: string; active: boolean }) {
  return (
    <Link to={to} className={`tab${active ? " active" : ""}`}>
      <span className="tab-glyph" aria-hidden="true">{glyph}</span>
      <span className="tab-label">{label}</span>
    </Link>
  );
}

export function BottomTabBar({ me, onLogout }: BottomTabBarProps) {
  const path = useLocation().pathname;
  const active = navActive(path);
  // The "More" sheet holds what doesn't fit four tabs (theme, showcase, account). It is
  // local UI state, not a route, so it lives here rather than in the router.
  const [moreOpen, setMoreOpen] = useState(false);

  return (
    <>
      {/* The sheet + its backdrop only exist while open. The backdrop closes on tap. */}
      {moreOpen && (
        <>
          <div className="more-backdrop" onClick={() => setMoreOpen(false)} aria-hidden="true" />
          <div className="more-sheet" role="dialog" aria-label="More">
            {me.identity && (
              <div className="account more-account">
                <div className="avatar" aria-hidden="true">
                  {me.identity.name.charAt(0).toUpperCase()}
                </div>
                <div>
                  <div className="account-name">{me.identity.name}</div>
                  <div className="account-role">{me.identity.role}</div>
                </div>
                <button
                  type="button"
                  className="account-logout"
                  aria-label="Log out"
                  title="Log out"
                  onClick={onLogout}
                >
                  ⏻
                </button>
              </div>
            )}
            {me.showcase_enabled && (
              <Link
                to="/showcase"
                className="showcase-link"
                onClick={() => setMoreOpen(false)}
              >
                <span aria-hidden="true">↗</span>
                <span>Public showcase</span>
              </Link>
            )}
            <ThemeSwitcher />
          </div>
        </>
      )}

      <nav className="bottom-tabbar" aria-label="Primary">
        <Tab to="/" glyph="◇" label="Projects" active={active.projects} />
        <Tab to="/todos" glyph="⊟" label="To-dos" active={active.todos} />
        <Tab to="/scheduling" glyph="◷" label="Schedule" active={active.scheduling} />
        {/* "More" is a menu, not a route — it toggles the sheet and shows active while open. */}
        <button
          type="button"
          className={`tab${moreOpen ? " active" : ""}`}
          aria-expanded={moreOpen}
          onClick={() => setMoreOpen((v) => !v)}
        >
          <span className="tab-glyph" aria-hidden="true">⋯</span>
          <span className="tab-label">More</span>
        </button>
      </nav>
    </>
  );
}
