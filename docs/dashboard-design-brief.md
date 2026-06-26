<!-- =========================================================================
docs/dashboard-design-brief.md
---------------------------------------------------------------------------
Responsible for: A frontend design brief describing everything the Orion
                 dashboard is expected to support, derived from the planned
                 features and the settled direction for what Orion is.
Role in project: Input for frontend/visual ideation (e.g. in a design tool).
                 It describes content, information architecture, surfaces,
                 states, audiences, and constraints. It does NOT prescribe the
                 visual design, which is the open part to explore. Source of
                 truth for direction: docs/orion-strategy.md, plans/orion-plan.md,
                 docs/e2-inc3-kickoff.md.
========================================================================= -->

# Orion Dashboard Design Brief

## Purpose of this document

This brief gathers, in one place, what the Orion dashboard is expected to contain and support
once its planned features land. It exists so the frontend look and layout can be explored from a
complete picture rather than from the current minimal dashboard alone. It describes the content
and the information architecture. It leaves the visual design open on purpose, because that is
the work this brief is meant to feed.

A note on status. Parts of what follows already exist (a working server-rendered dashboard).
Parts are planned and not yet built. The brief describes the intended end state so a design can
target it, and a later step re-fits Orion's backend around the chosen design. Section 12 marks
what is live today versus planned.

## What Orion is (the frame a designer needs)

Orion is a local-first tool that turns a developer's real project activity into readable
progress, and presents it to the developer and to a small set of trusted viewers (family,
supervisors). The activity comes from signals Orion reads: git history, a to-do or milestone
checklist, a status-aware tracker, hand-written notes, an idea incubator, and Claude Code
session summaries.

The defining idea is that Orion is a knowledge base that observes and reframes. It takes in the
projects, tasks, and ideas the developer is already working on, remembers them over time, and
presents a clearer view of them. It never authors plans. New ideas are written elsewhere (the
incubator), tasks live in the developer's own documents, and Orion reads and reframes them. This
shapes the whole interface, which is covered next.

## Core principles that shape the interface

1. **Observe and reframe, never originate.** The dashboard shows reframed observations. It is
   not a place to create or edit tasks, plans, or ideas. The one inbound action today is
   commenting, which is annotation on top of a report, not editing the underlying work.

2. **Read and comment, not write.** For now the dashboard is read plus comment. A read-write
   dashboard (editing state through the UI) is a deliberate future watershed and is out of scope
   for the near term. The layout can leave room for it without building it.

3. **A knowledge base with memory over time.** Orion remembers what the source documents said at
   each point in time, which lets it show change: progress, deadlines that moved, items that are
   slipping. The interface should be able to express not just current state but trend.

4. **Sectioned and extensible.** The dashboard is organized into distinct top-level sections, not
   one flat list. Projects are one section. A general to-do or milestone area is another.
   Scheduling and disciplines are planned sections. More sections will be added over time, so the
   layout should treat sections as a repeatable, growable pattern.

5. **Two lenses on the same content.** The same underlying knowledge base serves two purposes: a
   working dashboard for the developer and trusted viewers, and a curated showcase. The design
   should support both a full view and a curated subset (see Section 8).

6. **Legible to a third party.** A viewer who is not the developer (family, a supervisor) should
   be able to understand what is happening and, eventually, how the developer works. Clarity for
   an outside reader is a primary goal, not an afterthought.

## Audiences and roles

- **The developer (owner and admin).** Sees everything across all projects and sections. The
  primary daily user.
- **Trusted viewers (family, supervisors).** Log in with a personal access key and see only the
  projects and content they have been granted. Read and comment. This scoping already exists in
  the backend (per-user identity, an admin or viewer role, per-project read access).
- **A future public or guest showcase viewer.** A no-login, curated view for showing the work to
  a wider audience. Planned, not built. Relevant mainly to the showcase lens.

Design implication: the interface is role-aware. A scoped viewer sees a smaller dashboard than
the owner, with only their granted projects and sections present. There is a login surface, and
there is a clear signed-in identity (so comments are attributed to a real person, not a typed
name).

## Information architecture: the sections

The home is a set of top-level sections. The expected sections, in rough priority order:

1. **Projects.** Real software projects. Each is represented by a card that links to a full
   project page. This is the core of the dashboard.

2. **To-dos and general checklists.** Non-project trackers and general lists that are not a
   software project. The first real instance is an applications tracker (job and program
   applications). These must not look like project cards, because they are a different kind of
   thing. They are checklists the developer keeps, surfaced and reframed.

3. **Scheduling and time view (planned).** A forward view across general to-dos and
   project-specific tasks: what is due, and when. This is the time-oriented cut of the same items
   that appear in the other sections, gathered into one place.

4. **Disciplines and directions (planned, deferred).** Per-project and global descriptions of the
   conventions, design choices, and directions a project follows, and the reasons behind them.
   Observed from the developer's own instruction and design documents, not authored in Orion. Its
   value is making the work legible: a viewer can see how the developer orchestrates and builds,
   which is especially useful in the showcase lens.

5. **Cross-project connections (planned, longer range).** A view of how projects relate to one
   another, since Orion holds them all together. This is the knowledge-base payoff at the
   portfolio level. Longer range, but the design should not assume projects are always isolated
   islands.

The set is open-ended. New sections should slot in without redesigning the whole page.

## The surfaces (pages) and what each shows

### Home (portfolio overview)

The landing page. A sectioned overview. Within the Projects section, each project is a card
showing:

- Project name, linking to the project page.
- A one-line headline drawn from the latest update.
- Last activity as a relative time (for example, "2 days ago").
- Checklist progress, when the project has a checklist (for example, a "5 of 8 done" indicator).
- Forward signals from the planning layer: a count of at-risk items, and the nearest upcoming
  deadline, when present.

The To-dos section presents its trackers in their own format (not project cards). Other sections
appear as they are built. The home needs clear, friendly empty states for a fresh install and
for a viewer whose scope is narrow.

### Project page

The full view of one project. Expected blocks, roughly top to bottom:

- A header with the project name.
- A forward-looking block (the planning layer): milestones with progress and a nearest deadline,
  upcoming and overdue items, and slippage indicators where a deadline has moved. See Section 7
  for the states this block needs to express.
- The live checklist or task list for the project: each item with its done or open state, an
  optional due date shown as relative time, and at-risk or overdue treatment where it applies.
- A reports timeline: the history of progress updates, newest first, each with a short headline
  and a timestamp, each opening a full report.
- Comments from viewers, attributed to the person who wrote them.

The checklist and the forward block should render even when there are no reports yet, because
current state and plans are independent of report history.

### Report detail

One progress update in full. It shows the update body, often split into labelled sections (for
example, code activity, tasks, notes). It shows metadata: when it was generated and received,
which signals it came from, and who it was for. It shows the live checklist snapshot. It shows
the comment thread and a way to add a comment.

### General to-do or tracker page

The full view of a non-project tracker (for example, applications). It is a status-aware
checklist: each item carries a state beyond just done or open (for example, submitted, in
progress, not started), an optional deadline, and at-risk or overdue treatment. Items can be
grouped (for example, by milestone or by section). This surface is deliberately distinct from a
project page, because the thing it shows is a general list, not a software project.

### Login

A simple access-key login. The entry point for any viewer who is not on an open instance. After
login, identity is visible and carried through to comment attribution.

### Future surfaces

- A disciplines and directions view, per project and global.
- A cross-project connections view.
- A curated showcase view or mode (see Section 8).

## The forward-looking layer: states that need a visual language

This is the part most worth designing carefully, because it is about conveying time and risk at a
glance. The planning layer is derived from deadlines and status that already live in the source
documents, plus Orion's memory of how they changed.

Item states that need clear treatment:

- **Open** and **done** (the existing baseline).
- **Due soon.** Open, with a deadline approaching (a default window, for example within a week).
- **Overdue.** Open, with a deadline already passed.
- **At risk.** Due soon or overdue, gathered as the "needs attention" set.
- **Slipping.** A deadline that has moved later over time. This is a trend, not just a current
  value, so it benefits from a treatment that suggests motion or history.

Milestone states (a milestone is a derived grouping of items, for example a tracker section):

- Progress, as a proportion of items done.
- A nearest open deadline.
- An overall on-track or at-risk roll-up for the group.

Time rendering. Dates are best shown as relative time ("in 5 days", "3 days overdue") with the
absolute date available on closer inspection. There is already a relative-time mechanism that
works without scripting and upgrades to live relative labels when scripting is available.

Accessibility note. State should be legible without relying on color alone. The current approach
pairs a glyph and text with color, so the meaning survives for a color-blind reader or a plain
rendering. Keep that property in any new visual language.

## The two lenses: general use and showcase

The same content serves two framings, and the design should accommodate both.

- **General use.** The full sectioned dashboard for the developer and trusted viewers. Everything
  granted to that viewer is present.
- **Showcase or portfolio.** A curated subset whose contents depend on the purpose. When showing
  the developer's projects, it presents projects. When showing Orion itself as a portfolio piece,
  it can also present a sample of the general checklist and the disciplines view, to demonstrate
  the range of what Orion does. A showcase view is curated and outward-facing, so it favors
  clarity and selectiveness over completeness.

Practically, this suggests the design should support presenting a chosen subset of sections and
projects in a clean, outward-facing arrangement, distinct from the working dashboard.

## The content and data model available to a surface

A designer should know what data exists to draw on. Available, per project:

- Project identity and a list of progress reports over time, each with a body, labelled sections,
  a timestamp, and the signals it came from.
- A live checklist of items. Each item has text and a done state, and, as the planning layer
  lands, an optional deadline, an optional status beyond done or open, and an optional grouping
  (its milestone or section).
- Observed history: what items and deadlines looked like at earlier points, which is what makes
  slippage and progress-over-time expressible.
- Comments, each with an author identity, body, and time.

Across projects:

- The set of projects and, per viewer, which projects and content they may see.
- The basis for cross-project views, since all projects are held together.

## Cross-cutting requirements and constraints

- **Read and comment, with attributed identity.** No authoring of underlying work in the UI for
  now. Comments are attributed to the signed-in person.
- **Role-scoped visibility.** A viewer sees only granted projects and sections. The layout should
  degrade gracefully to a small, sparse view for a narrowly scoped viewer.
- **Accessibility.** State legible without color alone. Readable type. Works on a phone, since
  family will often view on a phone.
- **Safety of displayed text.** The dashboard renders text that originated from commit messages,
  task names, and comments, which can contain anything. All of it must be presented as inert
  text. This is a hard requirement, not a preference.
- **Graceful empty and first-run states.** A new install, a project with no reports yet, and a
  narrowly scoped viewer should all look intentional, not broken.
- **Time and timezone.** Times render in a configured display zone, with relative labels.
- **Theme.** A light and dark treatment is expected. The current build already distinguishes
  tokens for this.
- **Performance at portfolio scale.** The overview should stay fast and scannable as the number
  of projects grows.
- **Hosting and asset model.** Today the dashboard is a single server-rendered application with
  inline styles and scripts that are tightly locked down for security. A frontend redesign may
  move to a richer client and a different hosting model (a static or edge-hosted frontend is a
  natural fit). The designer does not need to honor the current asset constraints, since the
  re-fit can change them, but the security property (inert display of untrusted text) carries
  through any change.

## What the dashboard is deliberately not

- Not an authoring tool. Ideas are written in the incubator, tasks in the developer's own
  documents. The dashboard does not create or edit them.
- Not a planner that invents deadlines, milestones, or directions. It surfaces and reframes what
  already exists in the source material.
- Not a chat surface. Chat (Slack, Discord) is a separate, parked track. The dashboard is the
  primary visibility surface.

## Current state versus planned

Live today (server-rendered):

- A portfolio home with project cards (name, headline, last activity, checklist progress), which
  now also surfaces checklist-only trackers.
- A project page with the live checklist and the reports timeline.
- A report detail page with the body, sections, metadata, checklist, and comments.
- Login with per-user keys, an admin or viewer role, per-project read scope, and attributed
  comments.

Planned (this brief's target):

- The sectioned home (projects, to-dos, scheduling, disciplines, and beyond) in place of a single
  list.
- The forward-looking layer (due dates, at-risk, slipping, milestones) on items and projects.
- A clear separation of project surfaces from general to-do or tracker surfaces.
- The disciplines and directions view.
- Cross-project connections.
- A curated showcase view or mode, and eventually a no-login guest view.

The intent is to design toward the planned target, then re-fit Orion's data and rendering around
the chosen design.

## Open questions worth exploring in the design phase

- How sections are presented on the home: a single scrolling page with stacked sections, a tabbed
  or switchable layout, or a configurable arrangement.
- How a project card balances current state (progress) against forward signals (at-risk, nearest
  deadline) without becoming cluttered.
- How milestones and a time or schedule view relate visually, since they show the same items from
  different angles.
- How the curated showcase differs from the working dashboard: a separate mode, a separate route,
  or a filtered arrangement of the same components.
- How trend (slipping, progress over time) is shown without overwhelming the at-a-glance read.
- How the disciplines view reads for an outside viewer, since its whole purpose is legibility to a
  third party.
