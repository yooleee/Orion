<!-- =========================================================================
docs/skills-comb-rework-kickoff.md
---------------------------------------------------------------------------
Responsible for: The grounded kickoff for the NEXT step on the Skills tab —
                 reworking the E2 Inc 4 4c "skills comb" from a per-project,
                 component-flavored, bar-chart first build into a global,
                 resume-grade, true-to-the-Comb-Method showcase.
Role in project: Read this at the START of the skills-rework session, THEN decide
                 the approach (this doc captures the direction + open questions, not
                 a finished design — the research/decisions are the session's work).
                 Builds on: 4c shipped + deployed (PR #71). Current code: the seam in
                 docs/dashboard-api-contract.md (GET/POST /api/skills). Resume oracle:
                 ~/Applications/Documents/ygolding_resume_2026.md.
========================================================================= -->

# Kickoff: Skills tab rework — global, resume-grade, true comb

## Where we are

The Skills tab (E2 Inc 4 slice 4c) shipped and deployed as an **initial build**: a
per-project collector LLM-extracts skills from observed activity (git languages + commit
subjects + doc focus), the relay merges them across projects by name and derives a comb
"depth", and the SPA renders a comb band + evidence cards. It works end to end and is live.

On reviewing it **on real data**, the developer judged the current approach not yet
reasonable, for three specific reasons (this doc is the faithful record of that feedback).
**The research and design decisions below are the NEXT session's work — they were
deliberately not done in the build session.**

## The three problems (developer feedback, verbatim intent)

1. **Skills should be GLOBAL, not per-project.** Pulling skills per project is the wrong
   unit. It produced multiple entries that are essentially the same skill phrased
   differently across projects (e.g. "Python backends with CLI tooling" /
   "Python backends and integrations" / "Python backend development"), which the
   normalized-name merge does **not** collapse. The fix in spirit: **analyze all projects
   together and produce ONE global set of skills** from that analysis, rather than
   extracting per project and merging after.

2. **Show SKILLS, not project components.** Some current entries are not skills at all —
   they are a *system or component built for a project*. Example: "multi-drone tracking" is
   not a skill; it is a system built **using** skills (Bayesian probability, geospatial
   data, etc.). The tracking itself is a deliverable, not a competency. The bar to hit:
   skills should read like **what you would put on a resume**, or **what a job description
   lists as what it is looking for** — real, transferable competencies, not features.
   (It should still showcase what the developer can do and has done, just framed as skills.)

3. **The comb VISUAL is not true to the Comb Method.** The current rendering (vertical
   bars of varying height grouped by category) is not really what the Comb Method /
   Capability Comb is supposed to look like. This needs **research into the actual comb
   visual** before redesigning the rendering.

## Direction (to be decided, not yet settled)

- **Global extraction.** Replace per-project extraction with a **portfolio-level analysis**:
  gather evidence across all `skills = true` projects, then extract ONE deduplicated,
  global skill set. Open question: how to keep this **observe-not-originate** and
  **scope-aware** (a global extraction that fuses all projects cannot be cleanly
  scope-filtered per viewer — the per-project push was partly chosen *for* that; see the
  contract's scope-first note). Resolve the scope/global tension explicitly.
- **Resume-grade calibration.** Tune the extraction prompt (and possibly a
  classification/filtering pass) so output is competency-level, not component-level.
  Calibrate against the resume oracle `~/Applications/Documents/ygolding_resume_2026.md`
  and the language of real job descriptions. Decide whether a skill taxonomy / controlled
  vocabulary helps (vs free extraction).
- **True-to-method comb visual.** Research the Capability Comb's real visual form, then
  redesign `Skills.tsx` to match. The current depth→tooth-height mapping and the
  per-category band are placeholders.

## Parallelization / coupling (carried thought-process)

- The **`/api/skills` wire shape is the seam**. A global-extraction rework changes the
  PRODUCER side (one portfolio analysis instead of N per-project pushes) and likely the
  **store** (a global skill set, not per-project rows) and the **serializer** (less
  merging, more selection/filtering). The **SPA visual** rework is largely independent of
  the sourcing rework — it depends only on the (possibly revised) wire shape, so the two
  tracks can fan out once the new shape is agreed.
- **Scope handling is the genuine coupling risk**: a global skill set must still not leak
  an out-of-scope project's existence. Decide this before building, as it constrains the
  store/serializer shape (it is why the current design is per-project).

## What NOT to lose

- **Observe-not-originate stays the invariant.** A resume that is *derived from observed
  work*, never authored. Resume-grade framing must not become "author the skills you wish
  you had" — every skill stays grounded in evidence.
- The **evidence cards** (anchoring each skill to the projects + signals that demonstrate
  it) are the honesty mechanism — keep an equivalent, whatever the comb visual becomes.

## Pointers

- Current implementation: `src/orion/extract.py` (`_SKILLS_SYSTEM_PROMPT`, the resume-grade
  tuning target), `src/orion/collectors/skills.py`, `relay/api.py` (`serialize_skills`,
  the merge + depth), `web/src/routes/Skills.tsx` + `web/src/lib/skillsComb.ts`.
- Contract: [`docs/dashboard-api-contract.md`](dashboard-api-contract.md) (`GET /api/skills`).
- Known limitations recorded as **KI-26** (component-vs-skill + per-project duplication +
  comb-visual fidelity) and **KI-25** (the dropped project/tracker glyph) in
  [`known-issues.md`](known-issues.md).
- Resume oracle: `~/Applications/Documents/ygolding_resume_2026.md`.
