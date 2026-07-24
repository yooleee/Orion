// =============================================================================
// web/src/demo/demoData.ts
// -----------------------------------------------------------------------------
// Responsible for: The fabricated "sample-app" demo project shown on the public
//                  Showcase drill-down. Every value here is INVENTED — there is no
//                  real project, user, or activity behind it.
// Role in project: Lets a no-login guest click into the Showcase and see how Orion
//                  presents a monitored project (overview, working agreements, reports,
//                  discussion) using the REAL dashboard components, fed by these fixtures.
// Why fixtures (not a backend route): the Showcase is the one anonymous, internet-facing
//                  surface. Serving the demo from static, typed frontend data means there
//                  is NO anonymous backend path that can read real project data — a leak
//                  is impossible by construction, not merely guarded. (See the kickoff:
//                  docs/public-showcase-demo-kickoff.md.)
// Assumptions: shapes mirror web/src/api/types.ts exactly, so a contract change surfaces
//              here as a TypeScript error rather than a silent drift. Numbers are kept
//              coherent: checklist done/total == stats.progress == the card == report count.
//              Dates are fixed; relative-time labels ("3 days ago") drift over real time,
//              which is acceptable for a rarely-updated demo.
// =============================================================================

import type {
  ProjectDetail,
  ReportDetail,
  Section,
  ShowcaseCard,
} from "../api/types";

/** The demo project's route key and display name (kept in one place). */
export const DEMO_PROJECT_NAME = "sample-app";

/** Small typed helper so each report section is a [title, body] TUPLE (not string[]). */
const section = (title: string, body: string): Section => [title, body];

// --- The Showcase card (the clickable landing tile) -------------------------

export const DEMO_SHOWCASE_CARD: ShowcaseCard = {
  name: DEMO_PROJECT_NAME,
  description:
    "A sample project — a guided example of how Orion presents a monitored project.",
  status: "active",
  progress: { done: 5, total: 12, pct: 42 },
  report_count: 3,
};

// --- The project detail (Overview tab) --------------------------------------

export const DEMO_PROJECT: ProjectDetail = {
  name: DEMO_PROJECT_NAME,
  kind: "project",
  // The demo's blurb rides the About band, exactly like the real project page shows a
  // project's observed About line. (The old gap-5 `description` field retired in DR1-R U3.)
  about: "A small notes-and-tagging web app, used here purely as an example.",
  stats: {
    progress: { done: 5, total: 12, pct: 42 },
    next_due: { due_date: "2026-07-03", state: "due_soon" },
    reports_count: 3,
  },
  milestones: [
    {
      group: "Accounts & auth",
      done: 3,
      total: 4,
      at_risk: 1,
      nearest_due: "2026-07-03",
      slipping: false,
      slipping_count: 0,
    },
    {
      group: "Core features",
      done: 2,
      total: 5,
      at_risk: 1,
      nearest_due: "2026-07-05",
      slipping: false,
      slipping_count: 0,
    },
    {
      group: "Polish & launch",
      done: 0,
      total: 3,
      at_risk: 0,
      nearest_due: "2026-08-15",
      slipping: false,
      slipping_count: 0,
    },
  ],
  checklist: [
    // Accounts & auth (3/4 done)
    { text: "Email + password sign-up", done: true, due_date: null, key: "a1", group: "Accounts & auth", state: "done", status: "closed", slipping: false },
    { text: "Session cookies + CSRF", done: true, due_date: null, key: "a2", group: "Accounts & auth", state: "done", status: "closed", slipping: false },
    { text: "Password reset flow", done: true, due_date: null, key: "a3", group: "Accounts & auth", state: "done", status: "submitted", slipping: false },
    { text: "Google OAuth login", done: false, due_date: "2026-07-03", key: "a4", group: "Accounts & auth", state: "in_progress", status: "in_progress", slipping: false },
    // Core features (2/5 done)
    { text: "Create / edit / delete entries", done: true, due_date: null, key: "c1", group: "Core features", state: "done", status: "closed", slipping: false },
    { text: "Search & filters", done: true, due_date: null, key: "c2", group: "Core features", state: "done", status: "closed", slipping: false },
    { text: "Tagging", done: false, due_date: "2026-07-05", key: "c3", group: "Core features", state: "due_soon", status: "not_started", slipping: false },
    { text: "Export to CSV", done: false, due_date: null, key: "c4", group: "Core features", state: "not_started", status: "not_started", slipping: false },
    { text: "Bulk actions", done: false, due_date: null, key: "c5", group: "Core features", state: "not_started", status: "not_started", slipping: false },
    // Polish & launch (0/3 done)
    { text: "Empty & error states", done: false, due_date: null, key: "p1", group: "Polish & launch", state: "not_started", status: "not_started", slipping: false },
    { text: "Mobile layout pass", done: false, due_date: "2026-08-15", key: "p2", group: "Polish & launch", state: "upcoming", status: "not_started", slipping: false },
    { text: "Launch checklist", done: false, due_date: null, key: "p3", group: "Polish & launch", state: "not_started", status: "not_started", slipping: false },
  ],
  // Single-producer demo: no per-contributor cards (the section shows only at 2+ producers).
  producer_checklists: [],
  reports: [
    { id: 1003, number: 3, title: "OAuth underway; search shipped", generated_at: "2026-06-27T17:00:00+00:00", lane: "structured", share_level: "high_level", section_count: 3, author_name: "Developer", author_kind: "human", operated_by_name: null, source_tags: [] },
    { id: 1002, number: 2, title: "Core CRUD and search complete", generated_at: "2026-06-20T16:00:00+00:00", lane: "structured", share_level: "high_level", section_count: 3, author_name: "Developer", author_kind: "human", operated_by_name: null, source_tags: [] },
    { id: 1001, number: 1, title: "Auth foundation landed", generated_at: "2026-06-12T15:00:00+00:00", lane: "structured", share_level: "high_level", section_count: 3, author_name: "Developer", author_kind: "human", operated_by_name: null, source_tags: [] },
  ],
  // The demo project page shows the discussion thread read-only (no composer for a guest);
  // these two items illustrate the two-way loop. Both are fabricated.
  discussions: [
    { id: 1, author_name: "Reviewer", role: "supervisor", body: "How's the OAuth piece coming? Anything blocking the launch milestone?", created_at: "2026-06-26T09:00:00+00:00" },
    { id: 2, author_name: "Developer", role: "developer", body: "Token exchange works end to end — just polishing the callback error states, then it's done.", created_at: "2026-06-26T15:30:00+00:00" },
  ],
  // Unit 5: the "Working agreements" section — all of the project's observed principles
  // regardless of scope (project-specific + cross-project conventions), as the real project
  // page now shows them. Sources vary per card; the freshness stamp names the last push.
  disciplines: {
    updated_at: "2026-06-27T17:00:00+00:00",
    cards: [
      { title: "Secrets never leave the machine", why: "API keys and tokens live in a gitignored .env and are redacted before anything is logged or sent.", source: "docs/security.md" },
      { title: "Accessible by default", why: "Every interactive control is keyboard-reachable and labelled, and colour is never the only signal of state.", source: "docs/conventions.md" },
      { title: "Small, reviewable changes", why: "Each change ships as a small pull request with a clear description, so review stays fast and the history reads as a story.", source: "CONTRIBUTING.md" },
      { title: "Tests before merge", why: "New behavior lands with a test that pins it, and the suite is green before anything merges.", source: "docs/testing.md" },
    ],
  },
};

// --- Report details (one per timeline entry, nav linked) --------------------

export const DEMO_REPORTS: ReportDetail[] = [
  {
    id: 1001,
    number: 1,
    project: DEMO_PROJECT_NAME,
    title: "Auth foundation landed",
    sections: [
      section("SHIPPED", "Email + password sign-up, secure session cookies with CSRF protection, and a password-reset flow are all working end to end."),
      section("IN PROGRESS", "Starting on Google OAuth as an alternative sign-in."),
      section("NOTES", "Auth is the foundation everything else builds on, so it went first."),
    ],
    body: "Auth foundation landed. Email + password sign-up, secure session cookies, and password reset are done; OAuth is next.",
    lane: "structured",
    share_level: "high_level",
    generated_at: "2026-06-12T15:00:00+00:00",
    ingested_at: "2026-06-12T15:01:00+00:00",
    orion_version: "0.1.0",
    author_name: "Developer",
    author_kind: "human",
    operated_by_name: null,
    participants: [{ name: "Reviewer", role: null }],
    source_tags: [],
    checklist_snapshot: {
      done: 3,
      total: 12,
      rows: [
        { text: "Email + password sign-up", done: true, state: "done", due_date: null },
        { text: "Session cookies + CSRF", done: true, state: "done", due_date: null },
        { text: "Password reset flow", done: true, state: "done", due_date: null },
        { text: "Google OAuth login", done: false, state: "not_started", due_date: null },
      ],
    },
    nav: { prev_id: null, prev_number: null, next_id: 1002, next_number: 2 },
  },
  {
    id: 1002,
    number: 2,
    project: DEMO_PROJECT_NAME,
    title: "Core CRUD and search complete",
    sections: [
      section("SHIPPED", "Entries can be created, edited, and deleted, and search with filters is live."),
      section("IN PROGRESS", "Tagging is next, then export."),
      section("NOTES", "The core loop of the app now works; the rest is breadth and polish."),
    ],
    body: "Core CRUD and search complete. Entries support full create/edit/delete and search with filters; tagging is up next.",
    lane: "structured",
    share_level: "high_level",
    generated_at: "2026-06-20T16:00:00+00:00",
    ingested_at: "2026-06-20T16:01:00+00:00",
    orion_version: "0.1.0",
    author_name: "Developer",
    author_kind: "human",
    operated_by_name: null,
    participants: [{ name: "Reviewer", role: null }],
    source_tags: [],
    checklist_snapshot: {
      done: 4,
      total: 12,
      rows: [
        { text: "Create / edit / delete entries", done: true, state: "done", due_date: null },
        { text: "Search & filters", done: true, state: "done", due_date: null },
        { text: "Tagging", done: false, state: "due_soon", due_date: "2026-07-05" },
        { text: "Google OAuth login", done: false, state: "in_progress", due_date: "2026-07-03" },
      ],
    },
    nav: { prev_id: 1001, prev_number: 1, next_id: 1003, next_number: 3 },
  },
  {
    id: 1003,
    number: 3,
    project: DEMO_PROJECT_NAME,
    title: "OAuth underway; search shipped",
    sections: [
      section("SHIPPED", "Search shipped last week and is in daily use."),
      section("IN PROGRESS", "Google OAuth: token exchange works; finishing the callback error states."),
      section("NEXT", "Tagging, then the polish-and-launch milestone."),
    ],
    body: "OAuth underway; search shipped. Search is live; OAuth token exchange works and the callback handling is being finished.",
    lane: "structured",
    share_level: "high_level",
    generated_at: "2026-06-27T17:00:00+00:00",
    ingested_at: "2026-06-27T17:01:00+00:00",
    orion_version: "0.1.0",
    author_name: "Developer",
    author_kind: "human",
    operated_by_name: null,
    participants: [{ name: "Reviewer", role: null }],
    source_tags: [],
    checklist_snapshot: {
      done: 5,
      total: 12,
      rows: [
        { text: "Search & filters", done: true, state: "done", due_date: null },
        { text: "Google OAuth login", done: false, state: "in_progress", due_date: "2026-07-03" },
        { text: "Tagging", done: false, state: "due_soon", due_date: "2026-07-05" },
        { text: "Mobile layout pass", done: false, state: "upcoming", due_date: "2026-08-15" },
      ],
    },
    nav: { prev_id: 1002, prev_number: 2, next_id: null, next_number: null },
  },
];

/** Look up a demo report by its id (the route param), mirroring getReport(id). */
export function demoReportById(id: number | string): ReportDetail | undefined {
  const n = Number(id);
  return DEMO_REPORTS.find((r) => r.id === n);
}
