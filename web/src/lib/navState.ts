// =============================================================================
// web/src/lib/navState.ts
// -----------------------------------------------------------------------------
// Responsible for: Computing which primary section a route belongs to — the single
//                  source of truth for "active nav" shared by the desktop sidebar and
//                  the mobile bottom tab bar.
// Role in project: The shell has two navs (sidebar on desktop, bottom bar on mobile)
//                  that must light up the SAME section for a given route. Factoring the
//                  rule out here means the two can never drift (DRY) — a route that
//                  counts as "Projects" does so in both places by construction.
// =============================================================================

/** Which primary section is active for a route. A section owns a SET of related views
 *  (e.g. Projects is active on home, a project, and a report) — the design's rule. */
export interface NavActive {
  projects: boolean;
  todos: boolean;
  scheduling: boolean;
  disciplines: boolean;
  skills: boolean;
}

/**
 * Classify a path into its active primary section.
 *
 * Args:
 *   path: the current location pathname (e.g. "/project/orion").
 *
 * Returns: a NavActive with exactly the active section(s) flagged. In-shell routes map to
 *   at most one section; routes outside any section (none today) leave all false.
 *
 * Why: both navs call this, so "what's active" is decided once. Projects deliberately owns
 *   home + project + report (one nav item, a set of views); To-dos owns the trackers list
 *   and a tracker page; Scheduling owns its single view.
 */
export function navActive(path: string): NavActive {
  return {
    projects:
      path === "/" || path.startsWith("/project/") || path.startsWith("/report/"),
    todos: path === "/todos" || path.startsWith("/tracker/"),
    scheduling: path === "/scheduling",
    disciplines: path === "/disciplines",
    skills: path === "/skills",
  };
}
