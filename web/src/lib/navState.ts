// =============================================================================
// web/src/lib/navState.ts
// -----------------------------------------------------------------------------
// Responsible for: The SINGLE source of the shell's primary navigation — the ordered
//                  list of sections (NAV_ENTRIES) AND which section a route counts as
//                  active for (navActive), both used by the desktop sidebar and the
//                  mobile bottom tab bar.
// Role in project: The shell has two navs (sidebar on desktop, bottom bar on mobile) that
//                  must show the SAME sections and light up the SAME one for a given route.
//                  Factoring both the entry list and the active rule out here means the two
//                  renderers can never drift (DRY) — a new top-level section is one entry
//                  here (plus its route), not an edit in both components (DR1-R U2).
// =============================================================================

/** The primary sections. A section owns a SET of related views (Projects covers home, a
 *  project, a report, and a tracker), so this is the key, not one-route-per-key. */
export type NavKey = "projects" | "scheduling" | "search";

/** Which primary section(s) are active for a route — exactly one flag per NavKey. */
export type NavActive = Record<NavKey, boolean>;

/** One primary nav section. Carries everything both navs need to render it AND its own
 *  active rule, so the entry is the single definition of the section. The two renderers
 *  diverge only in shape (the sidebar shows label + a count/pill; the bottom bar shows a
 *  glyph + a short label), so both a `label` and a `shortLabel`/`glyph` live here. */
export interface NavEntry {
  key: NavKey;
  to: string;
  label: string; // desktop sidebar label
  shortLabel: string; // mobile bottom-bar label (may abbreviate the sidebar label)
  glyph: string; // mobile bottom-bar glyph (decorative; the label carries the meaning)
  /** Whether this section is active for a path — the design's route-ownership rule. */
  isActive: (path: string) => boolean;
}

export const NAV_ENTRIES: NavEntry[] = [
  {
    key: "projects",
    to: "/",
    label: "Projects",
    shortLabel: "Projects",
    glyph: "◇",
    // Projects deliberately owns home + a project + a report + a tracker (one nav item, a
    // set of views): the KB subsumes the progress views, so a tracker page reads as part of
    // the projects surface (the standalone To-dos section retired in the KB-surface Inc 1 IA).
    isActive: (path) =>
      path === "/" ||
      path.startsWith("/project/") ||
      path.startsWith("/report/") ||
      path.startsWith("/tracker/"),
  },
  {
    key: "scheduling",
    to: "/scheduling",
    label: "Scheduling",
    shortLabel: "Schedule",
    glyph: "◷",
    isActive: (path) => path === "/scheduling",
  },
  {
    // S2.3: cross-project search over the KB (reports + discussions). A first-class
    // section — one entry here lights up BOTH navs (the single-nav-source design), and
    // the search input lives on the page itself, so no shell chrome is added.
    key: "search",
    to: "/search",
    label: "Search",
    shortLabel: "Search",
    glyph: "⌕",
    isActive: (path) => path === "/search",
  },
];

/**
 * Classify a path into its active primary section(s).
 *
 * Args:
 *   path: the current location pathname (e.g. "/project/orion").
 *
 * Returns: a NavActive with exactly the active section(s) flagged. In-shell routes map to
 *   at most one section; routes outside any section (none today) leave all false.
 *
 * Why: both navs call this, so "what's active" is decided once — and it is derived from the
 *   SAME NAV_ENTRIES the navs render, so the entry list and the active rule can't disagree.
 *   The cast is sound because NAV_ENTRIES has exactly one entry per NavKey.
 */
export function navActive(path: string): NavActive {
  const result = {} as NavActive;
  for (const entry of NAV_ENTRIES) {
    result[entry.key] = entry.isActive(path);
  }
  return result;
}
