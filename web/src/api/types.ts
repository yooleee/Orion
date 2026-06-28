// =============================================================================
// web/src/api/types.ts
// -----------------------------------------------------------------------------
// Responsible for: The TypeScript shapes of the relay's read-only JSON API,
//                  mirroring docs/dashboard-api-contract.md one-to-one. This is
//                  the contract in code: the api client (client.ts) returns these
//                  types and every screen consumes them.
// Role in project: The SPA half of the SPA<->relay seam fixed in slice 4a.0. When
//                  the contract changes, change it here and in the doc together.
// Assumptions: timestamps are ISO 8601 UTC strings; deadlines are ISO YYYY-MM-DD
//              strings. The SPA formats relative time client-side from `display_tz`.
//              Status is a semantic enum; glyph/label/colour live in theme/status.ts.
// =============================================================================

/** Semantic item/milestone state. Presentation (glyph, label, colour) is owned by the frontend. */
export type Status =
  | "not_started"
  | "in_progress"
  | "done"
  | "due_soon"
  | "overdue"
  | "upcoming" // open & dated, but beyond the due-soon horizon (neutral, no glyph)
  | "at_risk" // union overdue-or-due-soon, used for roll-up counts
  | "slipping"
  | "on_track"; // roll-up state for a milestone or project row, not a per-item state

export type ProjectKind = "project" | "tracker";

/** The tracker's raw, first-class observed status (E2 Inc 4, gap 8). Distinct from the
 *  presentation `Status` enum: these are the producer's canonical item statuses. "submitted"
 *  and "closed" are both done; "in_progress" is the open state `state` alone could not carry. */
export type ItemStatus = "not_started" | "in_progress" | "submitted" | "closed";

export type Role = "admin" | "viewer";

/** done/total with a precomputed percentage (null when total is 0). */
export interface Progress {
  done: number;
  total: number;
  pct: number | null;
}

// --- GET /api/me ------------------------------------------------------------

export interface Identity {
  name: string;
  role: Role;
}

/** unrestricted => admin or open relay (projects null); else a scoped viewer's granted names. */
export interface Scope {
  unrestricted: boolean;
  projects: string[] | null;
}

export interface Me {
  gated: boolean;
  authenticated: boolean;
  identity: Identity | null;
  scope: Scope;
  display_tz: string;
  showcase_enabled: boolean; // true when the relay exposes the public Showcase (GET /api/showcase)
}

// --- GET /api/portfolio -----------------------------------------------------

/** The nearest open deadline and its state, or null when nothing open is dated. */
export interface NextDue {
  due_date: string;
  state: Status;
}

export interface ProjectSummary {
  name: string;
  kind: "project";
  headline: string;
  progress: Progress;
  at_risk: number;
  slipping: number;
  next_due: NextDue | null;
  updated_at: string;
  report_id: number | null;
}

/** Segmented progress-bar breakdown for the tracker card (the four tile the total). */
export interface Segments {
  overdue: number;
  due_soon: number;
  remaining: number; // open and not at risk
  done: number;
}

/** One at-risk item surfaced as a forward-signal chip on the tracker card. */
export interface AtRiskItem {
  state: Status; // overdue | due_soon
  label: string;
  due_date: string;
}

export interface TrackerSummary {
  name: string;
  kind: "tracker";
  item_count: number;
  progress: Progress;
  segments: Segments;
  at_risk: number;
  slipping: number;
  next_due: NextDue | null;
  at_risk_items: AtRiskItem[];
  updated_at: string;
}

export interface Portfolio {
  scope: Scope;
  projects: ProjectSummary[];
  trackers: TrackerSummary[];
}

// --- GET /api/projects/:name ------------------------------------------------

export interface ProjectStats {
  progress: Progress;
  next_due: NextDue | null;
  reports_count: number;
}

export interface Milestone {
  group: string;
  done: number;
  total: number;
  at_risk: number;
  nearest_due: string | null;
  slipping: boolean;
}

export interface ChecklistItem {
  text: string;
  done: boolean;
  due_date: string | null;
  key: string | null;
  group: string | null;
  // Derived per-row state: done | overdue | due_soon | in_progress | not_started. Deadline
  // urgency leads; in_progress fills the open-and-undated gap (E2 Inc 4 closed gap 8).
  state: Status;
  // The raw observed status (null for status-less items, e.g. table to-do rows). Carried
  // alongside `state` so the tracker's circular indicator can show the in-progress arc and
  // the submitted/closed label nuance independently of the single derived state.
  status: ItemStatus | null;
  slipping: boolean;
}

export interface ReportSummary {
  id: number;
  number: number; // per-project ordinal (#1 = oldest in this project); id stays the identity
  title: string; // the body headline, for the timeline entry
  generated_at: string;
  lane: string;
  share_level: string;
  section_count: number;
  source_tags: string[]; // [] in 4a (gap 4)
}

export interface Comment {
  id: number;
  author: string;
  role: Role | null; // null in 4a (gap 7)
  body: string;
  created_at: string;
}

export interface ProjectDetail {
  name: string;
  kind: ProjectKind;
  description: string | null; // null in 4a (gap 5)
  stats: ProjectStats;
  milestones: Milestone[];
  checklist: ChecklistItem[];
  reports: ReportSummary[];
  comments: Comment[];
}

// --- GET /api/reports/:id ---------------------------------------------------

export type Section = [title: string, body: string];

export interface Participant {
  name: string;
  role: Role | null; // null in 4a (gap 3)
}

export interface SnapshotRow {
  text: string;
  done: boolean;
  state: Status;
  due_date: string | null;
}

export interface ChecklistSnapshot {
  done: number;
  total: number;
  rows: SnapshotRow[];
}

export interface ReportNav {
  prev_id: number | null; // older neighbour — drives the link
  prev_number: number | null; // older neighbour's per-project ordinal — drives the label
  next_id: number | null; // newer neighbour — drives the link
  next_number: number | null; // newer neighbour's per-project ordinal — drives the label
}

export interface ReportDetail {
  id: number;
  number: number; // per-project ordinal (#1 = oldest in this project); id stays the identity
  project: string;
  title: string;
  sections: Section[];
  body: string;
  lane: string;
  share_level: string;
  generated_at: string;
  ingested_at: string;
  orion_version: string;
  participants: Participant[];
  source_tags: string[]; // [] in 4a (gap 4)
  checklist_snapshot: ChecklistSnapshot;
  comments: Comment[];
  nav: ReportNav;
}

// --- GET /api/scheduling ----------------------------------------------------

/** Where a scheduled deadline comes from — a project (◇) or a tracker (⊟). */
export interface ScheduleSource {
  name: string;
  kind: ProjectKind;
}

/** One open, dated deadline on the schedule. `state` is an open-deadline state
 *  (overdue | due_soon | upcoming); the SPA colours the time column by it. */
export interface ScheduleItem {
  state: Status;
  label: string;
  due_date: string;
  slipping: boolean;
  source: ScheduleSource;
}

/** The three time buckets, each sorted by due_date ascending (server-side). */
export interface ScheduleBuckets {
  overdue: ScheduleItem[];
  this_week: ScheduleItem[];
  later: ScheduleItem[];
}

export interface ScheduleSummary {
  overdue: number;
  due_this_week: number;
  slipping: number;
}

export interface SchedulingData {
  summary: ScheduleSummary;
  buckets: ScheduleBuckets;
}

// --- GET /api/disciplines ---------------------------------------------------

/** One observed principle card. `source` is the repo-relative doc it was observed in
 *  (the "observed · <source>" footer); it is caller-stamped by the producer, never
 *  model-chosen, so the claim is honest. The server drops the scope enum from the wire
 *  card — the Global vs project grouping already encodes it. */
export interface Discipline {
  title: string;
  why: string;
  source: string;
}

/** A project's group of project-specific principles, in the Disciplines section. */
export interface DisciplineGroup {
  name: string;
  principles: Discipline[];
}

/** GET /api/disciplines: principles split into a Global group (across all projects) and
 *  per-project groups. `scope` is the same viewer-scope block the portfolio ships. */
export interface DisciplinesData {
  scope: Scope;
  global: Discipline[];
  projects: DisciplineGroup[];
}

// --- GET /api/skills (the "skills comb") ------------------------------------

/** One merged competency in the skills comb (E2 Inc 4 slice 4c). DERIVED from observed
 *  activity, never authored. `depth` (1-4) is the comb tooth height — derived server-side
 *  from how central the skill is (evidence weight) and across how many projects (breadth).
 *  `projects` are the in-scope projects that evidence it (the honest "observed" anchor);
 *  `signals` name which kinds of evidence (git/tasks/docs) supported it. */
export interface Skill {
  name: string;
  category: string;
  depth: number;
  projects: string[];
  evidence: string;
  signals: string[];
}

/** GET /api/skills: the comb — a flat list of merged skills (ordered by category, then
 *  tallest tooth first) plus the category order to group them by. `scope` is the same
 *  viewer-scope block the portfolio ships. */
export interface SkillsData {
  scope: Scope;
  categories: string[];
  skills: Skill[];
}

// --- GET /api/showcase ------------------------------------------------------

/** A curated card's derived status pill: "shipped" at 100% done, else "active". */
export type ShowcaseStatus = "shipped" | "active";

/** One curated project on the public, no-login Showcase. SUMMARY FACTS ONLY — by design
 *  there is no checklist, report, comment, or deadline here (the public privacy boundary).
 *  `description` is the operator's curated blurb, or the observed headline, or "". */
export interface ShowcaseCard {
  name: string;
  description: string;
  status: ShowcaseStatus;
  progress: Progress;
  report_count: number;
}

export interface ShowcaseData {
  projects: ShowcaseCard[];
}

// --- POST /api/login, POST /api/logout --------------------------------------

export interface LoginResult {
  ok: boolean;
  user?: Identity;
}

export interface LogoutResult {
  ok: boolean;
}
