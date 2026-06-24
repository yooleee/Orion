# Feature Ideas from the Meta-Layer Session (2026-06-23)

**Reconciled 2026-06-23.** These directions surfaced during a session about the developer meta-layer
(the layered CLAUDE.md system, maintenance skills, an idea incubator, a portfolio map), not about
Orion directly. They have now been **reconciled against Orion's roadmap** — see the dated note
"Meta-layer feature ideas reconciled + dogfood captured (2026-06-23)" in
[`plans/orion-plan.md`](../plans/orion-plan.md) for the outcome and placements, and
[`docs/feature-ideas-reconciliation.md`](./feature-ideas-reconciliation.md) for the full per-idea
verdicts. The idea descriptions below are kept as the source context. A ready-to-paste session prompt
is at the bottom.

## Where these came from

A long session built and refined the user's developer "meta-layer": a layered CLAUDE.md system
(global plus thin-delta project files), two maintenance skills that audit those files against real
behavior, an incubator for brainstorming and graduating new project ideas, a `~/Developer/README.md`
portfolio map with per-project status, and a version-controlled `~/.claude` config. That structured
state is what made these Orion ideas obvious. Orion is the natural consumer of it.

## The reframe (read this first)

The session initially treated Orion as "git progress to a supervisor." Orion's own README corrects
that. Orion already ingests four signals (git, a TODO/milestone checklist, hand-written notes, and
Claude Code session summaries), routes to named recipients across Discord and Slack, and has a local
dashboard. Its CLAUDE.md marks local-first, single-user, and single-LLM as stage-appropriate, not
permanent, and keeps the report/intake as a portable summary-plus-metadata blob, seam-built toward
multi-party, hosted, shared collaboration.

So the right framing for the integration is: **Orion is a routing hub, and the meta-layer is
structured state it can read and route the right slice of to the right audience.** Most of the items
below are integrations, not new features.

## The ideas (reconcile against `plans/orion-plan.md` before acting on any)

### Strongest, because they land on seams Orion's CLAUDE.md already says it is holding open

1. **The incubator as a fifth signal.** Orion already reads structured files (tasks, notes). The
   incubator's `index.md` (ideas with a status of raw, refining, validated, graduated) is the same
   shape. A collector could emit idea-pipeline updates ("X reached validated", "Y graduated"). This
   fits Orion's modular-signal design and its rule against hardwiring git as the only input.

2. **Audience-typed routing.** This came straight out of the session. The user shares different
   things with different people: a supervisor wants progress, while family and mentors get the
   idea-and-design discussion. Orion already names recipients per project. Extend it to route signal
   *types* to audience *types*: a supervisor gets git and tasks, a mentor gets incubator and idea
   signals. This is the multi-party seam Orion is built toward, made concrete by a real pattern.

### Supporting

3. **Portfolio-aware `report --all`.** Orion has an `--all` mode. The portfolio map's status column
   (active, parked, archived) is the metadata to make it smart: skip parked and archived projects,
   feature active ones.

4. **`graduate-idea` emits an Orion intake event.** Graduation of an incubator idea is a milestone.
   The `graduate-idea` skill could auto-register the new repo in `orion.toml` and intake a "started
   project X" event, wiring the incubator's output straight into Orion through its existing intake path.

5. **Dashboard as the shareable meta-layer surface.** The later-horizon hosted dashboard is the
   natural home not just for report history but for the portfolio map and the idea pipeline, as one
   shareable "what I am building and considering" view. That doubles as an artifact to show family.

6. **An `orion status` backlog view.** Orion's sqlite already stores last-reported state per project.
   Surfacing "what is unreported across all projects" mirrors the catch-up-ledger pattern the session
   built for the `claude-md-update` skill, as a pre-report digest.

### Cheap and stylistic

7. **Summaries inherit the global Writing & Documentation Style.** Orion's summarizer prompt and the
   session-summary skill should produce lean, directional prose with no padded metric recaps. The
   behavior audit in the same session showed the user actively dislikes "5 PRs merged today" style
   recaps, and there is already a `session-openers-lean-directional` memory note to this effect.

## Meta-layer pieces an Orion session should read for full context

- `~/Developer/README.md`: the portfolio map, with per-project status and an Orion-tracked column.
- `~/Developer/incubator/`: the idea workspace (`index.md`, `_TEMPLATE.md`, the status field).
- `~/.claude/skills/graduate-idea/SKILL.md`: scaffolds a validated idea into a project (idea #4).
- `~/.claude/skills/claude-md-update/SKILL.md`: the harvest-ledger catch-up that idea #6 mirrors.
- `~/.claude/claude-md-toolkit/filter_transcript.py`: a cheap session digest, reusable for summaries.
- `~/.claude/CLAUDE.md`, the "Writing & Documentation Style" section, which is the style idea #7 refers to.

## Session prompt (copy this into the start of the dedicated Orion session)

> I want to fold some feature ideas into Orion's planning. Read `plans/orion-plan.md` (the roadmap
> and source of truth), then `CLAUDE.md`, `README.md`, and `docs/feature-ideas-meta-session.md` (the
> parking doc with the ideas and their context). Do not summarize them back to me.
>
> These ideas came from a separate meta-layer session and have not been reconciled against the
> roadmap. So first, for each one, tell me honestly whether it is already planned (point to where),
> adjacent to something planned, or genuinely new. Push back and flag your confidence. Drop or merge
> the ones already covered.
>
> Then, for the genuinely-new ones, focus-test the strongest first (I think #1 incubator-as-signal
> and #2 audience-typed routing, but tell me if you disagree): what is the real value, what is the
> open question or risk, and where it would sit in the horizon plan. Treat these as candidates to
> place in the roadmap, not work to start. No code yet.
>
> Keep Orion's invariants in mind: main is PR-gated, so any change goes via a branch and a PR.
> Security and leak-prevention come first. Commits carry no Co-Authored-By trailer.
