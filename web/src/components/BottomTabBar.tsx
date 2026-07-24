// =============================================================================
// web/src/components/BottomTabBar.tsx
// -----------------------------------------------------------------------------
// Responsible for: The mobile primary nav — a fixed bottom tab bar (Projects, Schedule,
//                  More) that replaces the desktop sidebar on narrow screens, plus a
//                  "More" sheet for the secondary actions (theme, showcase, account/logout)
//                  the sidebar's bottom block holds.
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
import { navActive, NAV_ENTRIES } from "../lib/navState";
import { ThemeSwitcher } from "./ThemeSwitcher";
import { AccountCard, ShowcaseLink } from "./NavSecondary";

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
          {/* The sheet holds the same secondary pieces as the sidebar's bottom block
              (NavSecondary), ordered account → showcase → theme for the mobile sheet. The
              showcase link closes the sheet as it navigates. */}
          <div className="more-sheet" role="dialog" aria-label="More">
            <AccountCard identity={me.identity} onLogout={onLogout} className="more-account" />
            {me.showcase_enabled && <ShowcaseLink onNavigate={() => setMoreOpen(false)} />}
            <ThemeSwitcher />
          </div>
        </>
      )}

      <nav className="bottom-tabbar" aria-label="Primary">
        {/* One Tab per NAV_ENTRIES entry (the single nav source): the bottom bar uses the
            short label + glyph. A new top-level section appears here automatically. */}
        {NAV_ENTRIES.map((entry) => (
          <Tab
            key={entry.key}
            to={entry.to}
            glyph={entry.glyph}
            label={entry.shortLabel}
            active={active[entry.key]}
          />
        ))}
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
