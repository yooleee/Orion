// =============================================================================
// web/src/components/Sidebar.tsx
// -----------------------------------------------------------------------------
// Responsible for: The fixed left sidebar — brand, SECTIONS nav, the PROJECTS and
//                  TRACKERS lists (from the portfolio), the theme switcher, and the
//                  account card with logout.
// Role in project: The persistent navigation half of the shell. Active highlighting is
//                  route-driven (Projects is "active" across home/project/report). The
//                  not-yet-built sections (Scheduling/Disciplines/Connections) appear as
//                  NEW-pill stubs so the IA is visible before those slices land.
// Security: project/tracker names come from the API (untrusted) and are rendered as
//           React text children — inert by construction (never dangerouslySetInnerHTML).
//           A guarantee-test pins this.
// =============================================================================

import { Link, useLocation } from "react-router-dom";
import type { Me, Portfolio } from "../api/types";
import { ThemeSwitcher } from "./ThemeSwitcher";

// The not-yet-built sections, shown as disabled NEW stubs (built in 4b/4c/4d).
const COMING_SOON = ["Scheduling", "Disciplines", "Connections"];

interface SidebarProps {
  me: Me;
  portfolio: Portfolio | null;
  onLogout: () => void;
}

export function Sidebar({ me, portfolio, onLogout }: SidebarProps) {
  const path = useLocation().pathname;

  const projects = portfolio?.projects ?? [];
  const trackers = portfolio?.trackers ?? [];

  // "Projects" is active across the project surfaces (home, a project, a report) — the
  // design's rule that a nav item owns a SET of related views.
  const projectsActive =
    path === "/" || path.startsWith("/project/") || path.startsWith("/report/");
  const todosActive = path === "/todos" || path.startsWith("/tracker/");

  return (
    <nav className="side" aria-label="Primary">
      <Link to="/" className="brand">
        <span className="brand-dot" aria-hidden="true" />
        <span>Orion</span>
      </Link>

      {/* SECTIONS */}
      <div className="side-group">
        <div className="side-group-label">Sections</div>
        <Link to="/" className={`nav-item${projectsActive ? " active" : ""}`}>
          <span>Projects</span>
          <span className="nav-count">{projects.length}</span>
        </Link>
        {/* To-dos / Trackers — only when the viewer has any (a scoped viewer may not). */}
        {trackers.length > 0 && (
          <Link to="/todos" className={`nav-item${todosActive ? " active" : ""}`}>
            <span>To-dos</span>
            <span className="nav-count">{trackers.length}</span>
          </Link>
        )}
        {COMING_SOON.map((name) => (
          <div key={name} className="nav-item disabled" aria-disabled="true">
            <span>{name}</span>
            <span className="pill-new">NEW</span>
          </div>
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
              }`}
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

      {/* Bottom block: theme switcher + account card (pushed down by the spacer). */}
      <div className="side-spacer" />

      <ThemeSwitcher />

      {me.identity && (
        <div className="account">
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
    </nav>
  );
}
