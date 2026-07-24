// =============================================================================
// web/src/components/Sidebar.tsx
// -----------------------------------------------------------------------------
// Responsible for: The fixed left sidebar — brand, SECTIONS nav, the PROJECTS and
//                  TRACKERS lists (from the portfolio), the theme switcher, and the
//                  account card with logout.
// Role in project: The persistent navigation half of the shell. Active highlighting is
//                  route-driven (Projects is "active" across home/project/report). The
//                  not-yet-built sections (Scheduling/Connections) appear as NEW-pill stubs
//                  so the IA is visible before those slices land.
// Security: project/tracker names come from the API (untrusted) and are rendered as
//           React text children — inert by construction (never dangerouslySetInnerHTML).
//           A guarantee-test pins this.
// =============================================================================

import { Link, useLocation } from "react-router-dom";
import type { Me, Portfolio } from "../api/types";
import { navActive, NAV_ENTRIES } from "../lib/navState";
import { ThemeSwitcher } from "./ThemeSwitcher";
import { AccountCard, ShowcaseLink } from "./NavSecondary";

interface SidebarProps {
  me: Me;
  portfolio: Portfolio | null;
  onLogout: () => void;
}

export function Sidebar({ me, portfolio, onLogout }: SidebarProps) {
  const path = useLocation().pathname;

  const projects = portfolio?.projects ?? [];
  const trackers = portfolio?.trackers ?? [];
  // S2.2: the SECTIONS count must match what clicking "Projects" actually shows — Home's
  // live Projects section, which excludes finished work. Counting past projects here would
  // read "2 projects" above a section listing one. The project LIST below still shows them
  // (a past project stays fully reachable, and going through the collapsed Home section to
  // reach one would be worse navigation), just muted so the list does not imply they are live.
  const activeProjectCount = projects.filter((p) => p.lifecycle !== "past").length;

  // Active-section state (shared with the mobile bottom bar via navActive, so the two
  // navs can't disagree about which section a route belongs to).
  const active = navActive(path);

  return (
    <nav className="side" aria-label="Primary">
      <Link to="/" className="brand">
        <span className="brand-dot" aria-hidden="true" />
        <span>Orion</span>
      </Link>

      {/* SECTIONS — one Link per NAV_ENTRIES entry (the single nav source). The trailing
          adornment stays sidebar-local and keyed on the section: Projects shows a live count
          (which needs the portfolio), Scheduling shows a NEW pill while it is still a stub. */}
      <div className="side-group">
        <div className="side-group-label">Sections</div>
        {NAV_ENTRIES.map((entry) => (
          <Link
            key={entry.key}
            to={entry.to}
            className={`nav-item${active[entry.key] ? " active" : ""}`}
          >
            <span>{entry.label}</span>
            {entry.key === "projects" && (
              <span className="nav-count">{activeProjectCount}</span>
            )}
            {entry.key === "scheduling" && <span className="pill-new">NEW</span>}
          </Link>
        ))}
      </div>

      {/* PROJECTS list */}
      {projects.length > 0 && (
        <div className="side-group">
          <div className="side-group-label">Projects</div>
          {projects.map((p) => (
            <Link
              key={p.name}
              to={`/project/${encodeURIComponent(p.name)}`}
              className={`proj-item${
                path === `/project/${encodeURIComponent(p.name)}` ? " active" : ""
              }${p.lifecycle === "past" ? " past" : ""}`}
            >
              {p.name}
            </Link>
          ))}
        </div>
      )}

      {/* TRACKERS list */}
      {trackers.length > 0 && (
        <div className="side-group">
          <div className="side-group-label">Trackers</div>
          {trackers.map((t) => (
            <Link
              key={t.name}
              to={`/tracker/${encodeURIComponent(t.name)}`}
              className={`proj-item${
                path === `/tracker/${encodeURIComponent(t.name)}` ? " active" : ""
              }`}
            >
              {t.name}
            </Link>
          ))}
        </div>
      )}

      {/* Bottom block: showcase link + theme switcher + account card (pushed down by the
          spacer). These pieces are shared with the mobile "More" sheet (NavSecondary); the
          sidebar orders them showcase → theme → account. */}
      <div className="side-spacer" />

      {/* The public Showcase link only appears when the relay actually exposes it
          (--showcase). Without it the link would lead to a 404 surface. */}
      {me.showcase_enabled && <ShowcaseLink />}

      <ThemeSwitcher />

      <AccountCard identity={me.identity} onLogout={onLogout} />
    </nav>
  );
}
