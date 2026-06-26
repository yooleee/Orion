# Handoff: Orion Dashboard

> **Project context (added on commit):** this folder is the committed **visual + behavioral spec**
> for the **E2 Inc 4** dashboard rebuild. Build plan and decisions:
> [`../docs/e2-inc4-dashboard-rebuild-kickoff.md`](../docs/e2-inc4-dashboard-rebuild-kickoff.md).
> Content/IA brief it realizes: [`../docs/dashboard-design-brief.md`](../docs/dashboard-design-brief.md).
> Settled stack: **React/Vite SPA, single-host on the relay** (relay → read-only JSON API). The
> `.dc.html` prototypes + `support.js` are reference only — **do not ship them** (per the handoff below).

## Overview
Orion is a **local-first progress tracker** that observes a developer's real activity — git history, checklists, and Claude Code sessions — and reframes it into readable progress. A guiding product principle runs through the whole design: **Orion observes and reframes; it never authors plans.** All content shown is derived from external sources (repos, tracking docs, sessions), never typed into Orion directly. The only things users create *in* Orion are comments.

This package documents a complete dashboard frontend: a portfolio home, project pages, a report reader, a general-purpose tracker, a forward-looking scheduling view, a disciplines/principles view, a cross-project connections graph, a public showcase/guest mode, login, role-scoped + empty states, and a full mobile pass — all themeable across **Dark / Sepia / Light**.

## About the Design Files
The files in this bundle are **design references created in HTML** — prototypes showing intended look and behavior. They are **not production code to copy directly.** They are authored in a lightweight in-house HTML component format (`.dc.html`, with a `support.js` runtime) purely so the prototype runs in a browser.

**Your task is to recreate these designs in Orion's actual codebase**, using its established framework, component library, routing, and styling conventions. If no frontend environment exists yet, choose the most appropriate stack for the project (e.g. React + a CSS-variable theme system, which maps very cleanly onto how these mocks are built) and implement there. **Do not ship the `.dc.html` files or `support.js`** — treat them as a precise visual + behavioral spec.

How to view the prototypes: open either `.dc.html` file in a browser.
- `Orion Dashboard - Themed.dc.html` — the **main desktop app**. Fully interactive: a left-sidebar **VIEW AS** switcher (Owner / Viewer / New), a **THEME** switcher (Dark / Sepia / Light), and clickable navigation between every desktop screen.
- `Orion Dashboard - Mobile.dc.html` — five **phone screens** on a pannable canvas (static layout references, not interactive).
- `explorations/` — the original three visual directions (A Atlas / B Field Notes / C Console) that led to the chosen design. **Reference only** — the chosen direction is "Field Notes layout + the color schemes," realized as the themed app. You do not need to implement the explorations.

## Fidelity
**High-fidelity (hifi).** Final colors, typography, spacing, and interaction states are all specified below and present in the files. Recreate the UI faithfully using the codebase's own libraries and patterns. Exact hex values, fonts, and measurements are given in **Design Tokens**.

---

## Global Layout & Shell

**Desktop** uses a fixed two-part shell:
- **Sidebar** — `236px` wide, fixed, full height. `padding: 26px 18px`. Background `--side`, right border `1px solid --sideborder`. Vertical flex column. Contains, top to bottom: brand lockup → `SECTIONS` nav group → `PROJECTS` list → `TRACKERS` list → (pushed to bottom via `margin-top:auto`) Public-showcase link, VIEW AS switcher, THEME switcher, account card.
- **Content area** — `flex:1`, `padding: 32px 40px 48px`, `min-width:0`. One screen renders at a time.

**Two screens break out of the shell** (full-bleed, no sidebar): **Login** and **Public Showcase**.

**Mobile** replaces the sidebar with:
- A compact **top app bar** (brand + avatar, or a `‹` back chevron + breadcrumb on subpages).
- A **bottom tab bar** — 4 tabs: Projects (`◇`), To-dos (`⊟`), Schedule (`◷`), More (`⋯`). Active tab uses `--accent` + weight 600; inactive `--tfaint`. Showcase has no bottom bar.
- Single-column content; all desktop rows/rails collapse to vertically stacked cards.

---

## Theming System (important — build this first)

Everything is driven by **CSS custom properties** set on a `[data-theme]` element. Three themes share one identical layout; only token values change. In React, set `data-theme` on a root wrapper and define the three token blocks in CSS; or use a ThemeProvider that injects these variables. A theme switch is purely swapping the attribute — no markup changes.

Default theme is **Sepia** (the user's preferred scheme). Transitions: backgrounds/borders animate `0.25s` on theme change (`transition: background .25s, border-color .25s`).

The three token sets are listed in full under **Design Tokens**.

---

## Screens / Views

### 1. Login  (full-bleed)
- **Purpose**: Sign in with a personal access key; user sees only the projects they've been granted.
- **Layout**: Centered column, `max-width: 392px`, vertically centered in viewport (`min-height:100vh; display:flex; align-items:center; justify-content:center`).
- **Components**:
  - Brand lockup (9px `--accent` dot + "Orion" in Newsreader 24/600), `margin-bottom: 30px`.
  - Card: `--panel` bg, `1px solid --border`, `border-radius: 16px`, `padding: 30px 28px`. Contains: H1 "Sign in" (Newsreader 24/600), subcopy (13.5px `--tlow`), `ACCESS KEY` mono label, a masked input field (`--panel2` bg, dots), a primary button (full-width, `--accent` bg, `--accent-ink` text, radius 10), a `DEMO` divider, and two demo buttons ("As owner" / "As family viewer").
  - Below the card: a theme switcher (Dark/Sepia/Light segmented control).
- **Behavior**: "Sign in" and "As owner" → owner dashboard home. "As family viewer" → viewer-scoped home.

### 2. Home — Portfolio Overview
- **Purpose**: The portfolio at a glance, split into top-level sections (the core IA decision: distinct sections, not one flat list).
- **Layout**: Eyebrow + H1 + sub, then stacked sections each introduced by a header row (`Newsreader 20/600` title + `1px` rule filling remaining width).
- **Sections**:
  - **Projects** — vertical stack of full-width project rows (`gap:12px`). Each row (`--panel`, `1px --border`, radius 13, `padding:17px 20px`, `cursor:pointer`, flex row align-center `gap:20px`):
    - Left (flex:1): project name (15.5/600 `--thi`) + one-line headline (13px `--tlow`).
    - Progress block (`width:128px`): mono `done/total` + `%` row, then a 5px track bar (`--track`) with an `--accent` fill (or `--ok` when 100%).
    - Forward block (`width:140px`): up to two stacked status signals (glyph + label, mono 10.5px, colored by state).
    - Timestamp (mono 11px `--tfaint`, `width:64px`, right-aligned).
  - **To-dos** — a single **tracker card** styled deliberately *differently* from project cards: `border-left: 3px solid --accent`, a `TRACKER` pill, the tracker name ("current focus"), an item count, a `done/total` figure, a segmented progress bar (overdue/due-soon/remaining), and a row of forward-signal chips ("▲ Hack Your Summer 2d overdue", "◷ Claude Corps Fellow in 6d", "+ 13 more →").
- **Behavior**: clicking a project row → Project page; clicking the tracker card → Tracker page.
- **Role scoping**: Viewer sees only granted projects (1), no To-dos/Trackers section, plus a scope banner ("You're viewing Yusuf's shared work · 1 project granted"). New install shows the empty state instead (see #9).

### 3. Project page
- **Purpose**: Everything observed about one project.
- **Layout**: Breadcrumb (`projects / orion`) → header row (H1 project name + one-line description on the left; three stat blocks PROGRESS / NEXT DUE / REPORTS on the right) → **two-column grid `1.55fr 1fr`, `gap:30px`**.
  - **Left column**:
    - **FORWARD LOOK** (mono section label) — stacked milestone cards. Each: title + right-aligned status signal(s); a progress bar + `done/total`. An at-risk milestone uses `border-color: --over` (instead of `--border`) to flag it. States shown: nearest/due-soon, at-risk + slipped (two signals), not-started.
    - **LIVE CHECKLIST** — rows with a 17px checkbox (filled `--accent` w/ `✓` when done; bordered square otherwise; `--over` border when overdue), the item label (strikethrough + `--tfaint` when done), and an optional right-aligned status pill.
  - **Right column**:
    - **REPORTS** — a vertical timeline (dot + connector line). Each entry: title + meta line (`#id · time ago · source tags`). Most-recent dot is `--accent`, older dots `--tfaint`. Ends with "view all 12 →". Entries are clickable → Report detail.
    - **COMMENTS** — comment cards (avatar + name + role/time + body) and a composer row ("Add a comment as {name}…" + "post"). Comments are the only user-authored content.

### 4. Report detail
- **Purpose**: Read one progress report in full. (Built to match a real Orion report example.)
- **Layout**: Breadcrumb (`projects / orion / report #26`) → header row (H1 "orion" + "Progress report #26 · 34m ago" on the left; "← Back to orion" and "Report #25 →" nav buttons on the right) → **two-column grid `1fr 308px`, `gap:32px`, `align-items:start`**.
  - **Left (body)**: a `--panel` card (radius 14, `padding:28px 30px`) containing the report title (Newsreader 19/600) and the body broken into **mono section labels** (`SHIPPED`, `DIRECTION`, `NOTES`) with prose underneath; `SHIPPED` is a bulleted list using em-dash markers in `--accent`. A closing italic status line sits above a top-bordered footer. Below the card: a **Comments** block (empty state "No comments yet.", "Commenting as {name}", a textarea placeholder, and a right-aligned "Post comment" button).
  - **Right (context rail)** — stacked `--panel` cards (radius 12), each with a mono label:
    - `DETAILS` — key/value rows: Report `#26`, Generated, Received, Lane (`structured` pill, accent), Detail (`high_level` pill, neutral), Version (`Orion 0.0.0`).
    - `SENT TO` — recipient rows (avatar + name + role): Alex (supervisor), Sam (supervisor).
    - `BUILT FROM` — signal chips: git history, checklist, session (each prefixed `◦` in accent).
    - `CHECKLIST SNAPSHOT` — label + `6/15`, a progress bar, then a few snapshot rows (done = `✓`/strikethrough, due-soon `◷`, overdue `▲`) and "+ 11 more".
- **Note**: an earlier version was a single narrow centered column; it was widened to this body+rail two-column layout specifically to use the horizontal space. Keep the rail.

### 5. Tracker page ("current focus")
- **Purpose**: A **general checklist that is not a project** — applications, learning, and build tasks reframed from a tracking doc. The name "current focus" is a placeholder; it is editable and the user may rename it.
- **Layout**: Breadcrumb (`to-dos / current focus`) → header (a `TRACKER` pill + "a general checklist, not a project" caption, H1 "current focus", description; right side shows `0/15 DONE` + a segmented progress bar) → a **legend** strip (the full state vocabulary as mono chips) → grouped checklists.
- **Groups** (each: a `Newsreader 17/600` header + rule + a group-level roll-up signal + `done/total · next due`): **Applications**, **Repo & white paper**, **Profile, learning & build** (last group is a 2-column grid of compact rows). Each row: a 18px **circular** status indicator (bordered = not started; `conic-gradient(--due X%, transparent 0)` = in progress; `--over` border = overdue), the task label (with a parenthetical type like "(job)"), and status pill(s).
- **Distinction from projects**: trackers use **circular** indicators and live under TRACKERS; projects use **square** checkboxes and progress bars. Keep this visual separation — it encodes the projects-vs-todos IA.

### 6. Scheduling (forward time-view)
- **Purpose**: Every deadline across projects and to-dos, gathered into one time-ordered view — the same items seen by *when* they're due.
- **Layout**: Eyebrow + H1 "Scheduling" + sub → a summary chip row (`▲ 3 overdue`, `◷ 2 due this week`, `↝ 1 slipping`) → three **time buckets**: `OVERDUE` (red dot + label), `THIS WEEK` (amber), `LATER` (neutral). Each bucket is a list of rows: a fixed-width relative-time column (mono, colored by urgency, `width:78px`), the item label, and a **source tag** on the right (`◇ project-name` in accent, or `⊟ current focus` neutral) so you can tell where each deadline comes from.

### 7. Disciplines & directions
- **Purpose**: How the work is built and why — conventions read from instruction/design docs, to make the work legible to an outside reader. Reinforces "observed, not authored."
- **Layout**: Eyebrow + H1 + sub → a **Global** section (conventions across all projects) then an **orion** section (project-specific), each a 2-column grid of principle cards. Each card (`--panel`, radius 13, `padding:18px 20px`): a bold title, a "why" paragraph, and a top-bordered footer reading `observed · <source-doc path>` (mono, `--tfaint`). Example principles: Local-first by default; Untrusted text is inert; Observe & reframe, never originate; Sectioned & extensible; State legible without colour alone; Two lenses on one base.

### 8. Cross-project connections
- **Purpose**: Show how projects relate — shared work, shared topics, which tracker items feed which project.
- **Layout**: Eyebrow + H1 + sub → a **graph panel** (`--panel`, radius 14, `height:300px`) with `orion` as a central accent node and `barebones-ai-village`, `sar_hackathon`, and `⊟ current focus` as satellite nodes. Edges drawn as SVG lines: **solid `--accent`** = "observes / feeds", **dashed `--tfaint`** = "shared thread". A small legend sits bottom-left. Below the graph: **relationship cards**, each `from → to` (or `↔`) with an explanation and a tag (`feeds` accent, or `shared topic` neutral).
- **Implementation note**: the SVG uses `stroke: var(--accent)` / `var(--tfaint)` so edges re-theme automatically. Use a real graph/SVG layer in production; node positions here are illustrative.

### 9. Empty / first-run state (New install)
- **Purpose**: What a fresh install shows before Orion has observed anything.
- **Layout**: Centered column (`max-width:520px`, `margin-top:80px`): a soft accent icon tile, H1 "Welcome to Orion", a paragraph explaining content appears once activity is observed, a dashed-border command hint (`orion watch ~/projects/my-repo` in mono), and a footer line "Orion observes & reframes — it never authors your plans." Sidebar shows projects count `0`, no project list.

### 10. Public Showcase / Guest mode  (full-bleed)
- **Purpose**: A curated, **read-only, no-sign-in** view for sharing work (e.g. with family or a supervisor).
- **Layout**: Top bar (brand + `SHOWCASE` pill on the left; a compact theme switcher of icon-only buttons + a "← Dashboard" link on the right). Centered content `max-width:1000px`:
  - **Hero** — centered: a mono eyebrow, a large Newsreader display H1 ("Work in progress, made legible.", 46px desktop / 29px mobile), and a sub.
  - **Selected projects** — 2-column grid of larger project cards (name + status pill, description, completion + report count, progress bar).
  - **How I work** — 3-column numbered cards (01/02/03) summarizing principles (Observe & reframe / Memory over time / Legible to anyone).
  - Footer: "A curated, read-only view · no sign-in required."

### Mobile screens (see `Orion Dashboard - Mobile.dc.html`)
Five phone references (`370px` screen inside a bezel), showing the adaptation rules: **Home** (sepia), **Project** (sepia), **Report detail** (dark — context rail collapses to inline chips + a snapshot bar), **Tracker** (sepia), **Showcase** (light, no bottom bar). Adaptation rules: sidebar → bottom tab bar; horizontal rows/rails → stacked cards; 3-up stat blocks stay in a row but shrink; type floors at ~12px; tap targets stay ≥44px.

---

## Interactions & Behavior
- **Navigation** (desktop): sidebar items and in-content cards/rows are click targets. `Projects` nav + brand → Home; project rows/sidebar project items → Project; report timeline entries → Report; `To-dos`/tracker card → Tracker; `Scheduling`/`Disciplines`/`Connections` nav → those views; `↗ Public showcase` → Showcase; account card (⏻) → Login. Breadcrumbs are back-links.
- **Active-nav highlighting**: a nav item is "active" for a set of related views (e.g. Projects nav is active on Home, Project, and Report). Active sidebar item: `--nav-active-bg` background + `1px --border` + `--thi` text + weight 600. Active project/tracker list item: `--proj-active-bg` + `--accent` text + 600.
- **Theme switch**: swaps `data-theme`; backgrounds/borders transition `0.25s`. Segmented control: the active segment is `--accent`/`--accent-ink`/600; inactive is transparent/`--tlow`/500.
- **View-as switch** (prototype affordance): toggles Owner / Viewer / New, which changes the visible sidebar groups, project list, account identity (Yusuf · Admin vs Mum · Viewer), and Home content. In production this is **driven by the authenticated user's role + grants**, not a manual switch — but the three resulting states (full / scoped / empty) are real and must be built.
- **Status vocabulary** (used everywhere): each state is always **glyph + label + color**, never color alone (accessibility requirement, and an observed product principle). See tokens.
- **Hover/active** (apply codebase conventions): cards and rows that navigate use `cursor:pointer`; give them a subtle hover (e.g. border/elevation shift). The prototype keeps hovers minimal — match your design system.
- **Content is read-only** except comments. There are no create/edit/delete affordances on projects, tasks, or reports.

## State Management
- `theme`: `'dark' | 'sepia' | 'light'` — global, persisted (default `'sepia'`). Drives `data-theme`.
- `view` / route: which screen is shown (`home`, `project`, `report`, `tracker`, `schedule`, `disciplines`, `connections`, `showcase`, `login`). Use real routing in production.
- `scenario` / auth context: `'owner' | 'viewer' | 'new'` in the prototype → in production, derived from the **authenticated user + their project grants**. Controls sidebar groups, project list, identity, and whether Home shows full / scoped / empty content.
- **Data**: all domain data (projects, checklists, milestones, reports, comments, tracker items, deadlines, principles, connections) is **observed from external sources** and read-only in the UI. Plan for: per-project checklist + milestones + reports + comments; a tracker entity (general checklist, groupable) separate from projects; a derived cross-project deadline list (Scheduling) and connection graph.

## Design Tokens

### Typography
- **Hanken Grotesk** — primary UI/body (weights 400/500/600/700).
- **Newsreader** — serif, for H1s and section titles (weights 400/500/600). Gives the "editorial / Field Notes" feel.
- **Spline Sans Mono** — metadata, labels, counts, status glyphs, eyebrows (weights 400/500/600).
- Recurring sizes: display H1 30–32px/600 (Newsreader); showcase hero 46px; section title 20px/600 (Newsreader); card title 14–15.5px/600; body 13–14.5px; meta/labels 10–11px (mono, letter-spacing ~0.1–0.13em uppercase). Mobile floors body ~12px.
- Headings use `letter-spacing: -0.01em` to `-0.02em`.

### Status vocabulary (glyph · meaning · token)
- `○` not started · `--todo`
- `◐` in progress · `--due`  (also drawn as `conic-gradient(--due X%, transparent 0)` on circular indicators)
- `✓` done / submitted / on track · `--ok`
- `◷` due soon · `--due`
- `▲` overdue · `--over`
- `△` at risk · `--over`
- `↝` slipping / slipped · `--slip`
- Source tags: `◇` = project, `⊟` = tracker.

### Theme token sets
Each theme defines the same variables. (Mobile uses an identical set.)

**Dark**
```
--bg:#1b1a17;  --side:#171612;  --panel:#211f1b;  --panel2:#26241f;
--border:#322e28;  --sideborder:#272420;
--thi:#f2efe9;  --tmid:#a8a39a;  --tlow:#8a857c;  --tfaint:#6f6a61;
--accent:#93b8a0;  --accent-soft:rgba(147,184,160,.14);  --accent-ink:#1b1a17;
--track:#2c2a25;  --nav-active-bg:#211f1b;  --proj-active-bg:rgba(147,184,160,.12);  --rowb:#242220;
--due:#d9b56a;   --due-bg:rgba(217,181,106,.13);
--over:#e0876f;  --over-bg:rgba(224,135,111,.13);
--slip:#c99fc0;  --slip-bg:rgba(201,159,192,.13);
--ok:#93b8a0;    --ok-bg:rgba(147,184,160,.14);
--todo:#9a948a;  --todo-bg:#26241f;
```

**Sepia** (default)
```
--bg:#f4f1ea;  --side:#ece7dc;  --panel:#ffffff;  --panel2:#faf8f3;
--border:#e3ddd0;  --sideborder:#ddd6c7;
--thi:#26231d;  --tmid:#5b564b;  --tlow:#6b665c;  --tfaint:#9a9384;
--accent:#5a6b8c;  --accent-soft:#e6e9f0;  --accent-ink:#ffffff;
--track:#e8e2d5;  --nav-active-bg:#ffffff;  --proj-active-bg:#e6e9f0;  --rowb:#e8e2d5;
--due:#a8843a;   --due-bg:#f4ecda;
--over:#bd6a4a;  --over-bg:#f6e6df;
--slip:#9163a8;  --slip-bg:#efe6f2;
--ok:#5a8c6b;    --ok-bg:#e6f0ea;
--todo:#9a9384;  --todo-bg:#efeadf;
```

**Light**
```
--bg:#f6f7f9;  --side:#eef0f3;  --panel:#ffffff;  --panel2:#f4f6f8;
--border:#e4e7ec;  --sideborder:#e1e4e9;
--thi:#191c22;  --tmid:#5a626e;  --tlow:#6b727e;  --tfaint:#99a0ab;
--accent:#4a6491;  --accent-soft:#e7ecf4;  --accent-ink:#ffffff;
--track:#e9ebef;  --nav-active-bg:#ffffff;  --proj-active-bg:#e7ecf4;  --rowb:#edeff2;
--due:#b07d2a;   --due-bg:#f7efdd;
--over:#c2603f;  --over-bg:#f9e8e1;
--slip:#8a52a8;  --slip-bg:#f1e8f5;
--ok:#3f8a6f;    --ok-bg:#e4f1ea;
--todo:#8a909a;  --todo-bg:#eef0f3;
```

Token roles: `--bg` page; `--side` sidebar/nav surfaces; `--panel` cards; `--panel2` insets/fields; `--border` hairlines; `--sideborder` sidebar edge; `--thi/--tmid/--tlow/--tfaint` text high→faint; `--accent` brand accent (differs per theme: sage in Dark, slate-blue in Sepia/Light); `--accent-soft` accent-tint backgrounds; `--accent-ink` text/icon on accent fills; `--track` progress-bar troughs; `--rowb` list-row dividers; `--nav-active-bg`/`--proj-active-bg` active nav states; status pairs `--<state>` (foreground) + `--<state>-bg` (tint).

### Shape, spacing, motion
- **Radius**: cards 13–14px; rail/secondary cards 11–12px; pills/chips 5–8px; progress bars/indicators 99px (pill); status checkboxes 5px (square) / 99px (circular tracker). Sidebar nav items 9px. Phone bezel 46px, screen 36px.
- **Borders**: hairline `1px solid --border`; flag borders swap to `--over` (at-risk) or use `border-left: 3px solid --accent` (tracker card).
- **Progress bars**: height 4–6px, `--track` trough, fill `--accent`/`--ok`, or **segmented** (multiple colored widths for overdue/due-soon/remaining).
- **Spacing**: content padding `32px 40px`; card padding `14–30px`; common gaps 9/12/16/24/30px.
- **Shadows**: minimal on desktop; the showcase project cards and login card may carry a soft shadow. Phone bezels use a dark frame, not shadow.
- **Motion**: theme transition `0.25s` on bg/border. Keep other transitions subtle.

## Assets
- **No raster/image assets.** All iconography is **Unicode glyphs** (`○ ◐ ✓ ◷ ▲ △ ↝ ◇ ⊟ ◦ ‹ ↗ ⏻ ⋯`) set in Spline Sans Mono. In production, you may swap these for your icon library — but preserve the glyph-shape semantics so state stays legible without color.
- **Fonts**: Google Fonts — Hanken Grotesk, Newsreader, Spline Sans Mono. Self-host or load per your codebase conventions.
- Avatars are initial-in-circle placeholders (`--accent` bg, `--accent-ink` text).

## Files
- `Orion Dashboard - Themed.dc.html` — main desktop app: all desktop screens, the 3 themes, and the Owner/Viewer/New scenarios. **Primary reference.**
- `Orion Dashboard - Mobile.dc.html` — five mobile screen references on a canvas.
- `explorations/Orion Dashboard - Explorations (A-B-C).dc.html` — the three original visual directions (A Atlas dark / B Field Notes light / C Console). Historical context only; the chosen direction is the themed app above.
- `support.js` — the prototype runtime. **Do not port** — present only so the `.dc.html` files render in a browser.

To preview: open a `.dc.html` file directly in any modern browser. In the themed file, use the sidebar **VIEW AS** and **THEME** switchers and click around to exercise every screen and state.

## Screenshots
Rendered references in `screenshots/` (PNG). Desktop shots are full-app captures (sidebar + content) at the default Sepia theme unless noted; theme shots show the same layout re-tokenized.
- `desktop-01-home-sepia.png` — Home / portfolio overview (owner)
- `desktop-02-project-sepia.png` — Project page
- `desktop-03-report-sepia.png` — Report detail (body + context rail)
- `desktop-04-tracker-sepia.png` — Tracker ("current focus")
- `desktop-05-scheduling-sepia.png` — Scheduling (deadline buckets)
- `desktop-06-disciplines-sepia.png` — Disciplines & directions
- `desktop-07-connections-sepia.png` — Cross-project connections
- `desktop-08-showcase-light.png` — Public showcase / guest mode (full-bleed)
- `desktop-09-login-sepia.png` — Login (full-bleed)
- `desktop-10-empty-new-install.png` — Empty / first-run state
- `desktop-11-viewer-scoped.png` — Family-viewer scoped home (1 granted project)
- `theme-01-home-dark.png` — Home in Dark theme
- `theme-02-report-dark.png` — Report detail in Dark theme
- `theme-03-home-light.png` — Home in Light theme
- `mobile-all-screens.png` — All five mobile screens (Home, Project, Report·dark, Tracker, Showcase)
