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

/** Whether a project is still running or finished (S2.2).
 *
 *  A DECLARED fact — an admin sets it with `relay-project lifecycle` — never derived from how
 *  long a project has been quiet, because quiet is not finished. The relay defaults a missing
 *  or NULL value to "active", so this is never null on the wire.
 *
 *  Distinct from the Showcase's `shipped`, which is derived from 100% completion at read time.
 *  Completion and lifecycle are different facts and must not be conflated. */
export type ProjectLifecycle = "active" | "past";

/** The tracker's raw, first-class observed status (E2 Inc 4, gap 8). Distinct from the
 *  presentation `Status` enum: these are the producer's canonical item statuses. "submitted"
 *  and "closed" are both done; "in_progress" is the open state `state` alone could not carry. */
export type ItemStatus = "not_started" | "in_progress" | "submitted" | "closed";

/** A relay account's role.
 *
 *  "member" (auth revamp, Unit 5) is the read-only ORG INSIDER: it sees every org-visible
 *  project without a per-project grant, and can never write anything. The SPA needs no
 *  special case for it — scope arrives already resolved in `/api/me`, and every write
 *  surface is server-denied. */
export type Role = "admin" | "viewer" | "supervisor" | "member";

/** A discussion item's thread role (E2 Inc 5) — distinct from the relay_users `Role`.
 *  "orion" is reserved for a later grounded-responder rung and never appears this phase
 *  (observe-not-originate: Orion authors nothing). */
export type DiscussionRole = "supervisor" | "developer" | "orion";

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
  // S2.2: "past" groups this row into Home's collapsed "Past projects" section. The relay
  // ALSO strips its forward-looking urgency (at_risk / slipping → 0, next_due → null), so
  // the fields below are already suppressed and no consumer can reconstruct an overdue read.
  lifecycle: ProjectLifecycle;
  headline: string;
  progress: Progress;
  at_risk: number;
  slipping: number;
  next_due: NextDue | null;
  updated_at: string;
  report_id: number | null;
  // KB surface (Unit 2): the observed "About" line (what the project is), or null when the
  // project set no about_file. Rendered as the Home row sub-line; the headline stays the
  // latest-report pulse. A distinct concept from a report headline.
  about: string | null;
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
  // S2.2, same rule as ProjectSummary. A past tracker also arrives with `at_risk_items` empty
  // and its `segments` overdue/due_soon folded into `remaining`, so its bar carries no colour
  // it has not earned.
  lifecycle: ProjectLifecycle;
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
  // How many of this group's OPEN items are slipping (E1.2 Unit 5). The card shows the
  // count only when >1 ("2 slipped"); `slipping` alone still drives the 0/1 rendering.
  slipping_count: number;
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

/** What KIND of producer pushed a report (auth revamp, Unit 4a).
 *
 *  `null` is a THIRD state, not a synonym for "human": it means the push carried no
 *  resolvable identity at all — a legacy anonymous push, or an account since deleted.
 *  The UI badges only "agent"; "human" and null both render as they always have. */
export type AuthorKind = "human" | "agent" | null;

export interface ReportSummary {
  id: number;
  number: number; // per-project ordinal (#1 = oldest in this project); id stays the identity
  title: string; // the body headline, for the timeline entry
  generated_at: string;
  lane: string;
  share_level: string;
  section_count: number;
  author_name: string | null; // C3 Inc 2: producer who pushed it; null for legacy/older reports
  author_kind: AuthorKind; // Unit 4a: badge an agent's push; null when unattributed
  operated_by_name: string | null; // Unit 4a: for an agent, the human it acted for
  source_tags: string[]; // [] in 4a (gap 4)
}

/** One entry in a project's two-way supervisor↔developer discussion thread (E2 Inc 5) —
 *  the single conversation surface since KI-28 Stage 2 retired per-report comments.
 *  Attribution is first-class and server-derived: `author_name` (never a client-typed
 *  name) and a REAL `role`. */
export interface DiscussionItem {
  id: number;
  author_name: string;
  role: DiscussionRole;
  body: string;
  created_at: string;
}

/** One identified producer's own live checklist (C3 Inc 2) — the same item shape as the
 *  aggregate `checklist`, plus its own progress and the producer's server-derived name.
 *  The SPA renders one card per producer only when there are two or more. */
export interface ProducerChecklist {
  author_name: string;
  progress: Progress;
  items: ChecklistItem[];
}

/** One observed principle card. `source` is the repo-relative doc it was observed in
 *  (the "observed · <source>" footer); it is caller-stamped by the producer, never
 *  model-chosen, so the claim is honest. The server drops the scope enum from the wire
 *  card — on the project page every one of a project's cards renders regardless of scope. */
export interface Discipline {
  title: string;
  why: string;
  source: string;
}

/** A project's "Working agreements" (Unit 5): all of its observed discipline cards plus the
 *  ISO timestamp the relay last received them (the section's "updated <date>" freshness
 *  line). null on ProjectDetail when the project has never pushed disciplines. */
export interface ProjectDisciplines {
  cards: Discipline[];
  updated_at: string;
}

export interface ProjectDetail {
  name: string;
  kind: ProjectKind;
  // S2.2: "past" badges the page header and means `stats.next_due` arrives null. The
  // suppression stops there BY DESIGN — `milestones` and `checklist` keep their real dates
  // and states, because those are the record of what happened and they sit behind a
  // collapsed group rather than in a headline.
  lifecycle: ProjectLifecycle;
  // KB surface (Unit 2): the observed "About" line under the title, or null when unset —
  // mechanically observed from the project's doc, not an authored blurb. (The always-null
  // `description` gap-5 field retired in DR1-R U3; ShowcaseCard.description is a separate,
  // real field and is unaffected.)
  about: string | null;
  stats: ProjectStats;
  milestones: Milestone[];
  checklist: ChecklistItem[];
  producer_checklists: ProducerChecklist[]; // C3 Inc 2: per-contributor checklists
  reports: ReportSummary[];
  discussions: DiscussionItem[];
  disciplines: ProjectDisciplines | null; // Unit 5: "Working agreements" section, or null
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
  author_name: string | null; // C3 Inc 2: producer who pushed it; null for legacy/older reports
  author_kind: AuthorKind; // Unit 4a: badge an agent's push; null when unattributed
  operated_by_name: string | null; // Unit 4a: for an agent, the human it acted for
  participants: Participant[];
  source_tags: string[]; // [] in 4a (gap 4)
  checklist_snapshot: ChecklistSnapshot;
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

// --- GET /api/search?q= (S2.3 / KB Inc 3) ------------------------------------

/** One report hit. `title` is the server's `_headline` (same rule as the timeline row
 *  the hit links to); `snippet` is a plain-text substring of the stored body around the
 *  first match — NOT HTML-escaped by the server, so it must only ever be rendered as
 *  React text children (escape-before-highlight). */
export interface SearchReportHit {
  id: number;
  project: string;
  title: string;
  generated_at: string;
  snippet: string;
}

/** One discussion hit. Same snippet rule as SearchReportHit. */
export interface SearchDiscussionHit {
  id: number;
  project: string;
  author_name: string;
  role: DiscussionRole;
  created_at: string;
  snippet: string;
}

/** One result class: its hits (newest first) + its OWN cap flag. `capped: true` means
 *  more matches existed beyond the server's per-class cap (50) — the UI must say so
 *  rather than silently truncating (the no-silent-states rule, on the wire). */
export interface SearchResultClass<Hit> {
  hits: Hit[];
  capped: boolean;
}

/** GET /api/search response: the echoed query + the two DISTINCT result classes
 *  (reports vs discussions are different things; the shape never flattens them). */
export interface SearchResults {
  query: string;
  reports: SearchResultClass<SearchReportHit>;
  discussions: SearchResultClass<SearchDiscussionHit>;
}
