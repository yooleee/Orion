// =============================================================================
// web/src/routes/Skills.tsx
// -----------------------------------------------------------------------------
// Responsible for: The Skills section — a "partial living resume" built as a Comb
//                  (E2 Inc 4 slice 4c; 4c-rework CP2 = the true comb-shaped-skills form).
//                  A horizontal spine (breadth) with "teeth" hanging DOWN from it, each
//                  tooth's LENGTH encoding the skill's derived depth, grouped into labelled
//                  category segments, with evidence cards below that anchor every skill to
//                  the projects that demonstrate it.
// Role in project: The developer-view "what I work on, shown from real work" lens. Reads
//                  /api/skills (the server merges each project's observed skills across the
//                  portfolio and derives depth). REPLACES the design's desktop-07 connections
//                  graph (the projects are mostly independent; a skills comb is the more
//                  honest, useful fit). Built in-code against the tokens — no screenshot oracle.
// Observe-not-originate: every skill is DERIVED from observed activity (git languages +
//                  commit subjects + doc focus), never authored; the evidence card's
//                  "observed · <projects>" footer is the honest anchor.
// Security: skill name / evidence / project names are observed text from the API
//           (untrusted) and render as React text children — inert by construction (never
//           dangerouslySetInnerHTML). A guarantee-test pins this.
// =============================================================================

import type { Skill } from "../api/types";
import { getSkills } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { groupByCategory, toothHeight } from "../lib/skillsComb";

/** A human label for each evidence-signal kind (the closed set the wire carries). */
const SIGNAL_LABELS: Record<string, string> = {
  git: "git",
  tasks: "to-dos",
  docs: "docs",
};

/** One comb "tooth": a bar hanging DOWN from the spine whose LENGTH encodes the skill's
 *  derived depth, with the skill name beneath. The name is always shown (legible without
 *  colour or length alone), and the title attribute carries the same text on hover. */
function CombTooth({ skill }: { skill: Skill }) {
  return (
    <div className="comb-col" title={`${skill.name} — depth ${skill.depth}/4`}>
      <div className="comb-bar-track">
        <div
          className="comb-bar"
          style={{ height: `${toothHeight(skill.depth)}px` }}
          aria-hidden="true"
        />
      </div>
      <div className="comb-name">{skill.name}</div>
    </div>
  );
}

/** One evidence card: the skill name, the observed evidence sentence, and a footer
 *  anchoring it to the projects that demonstrate it plus which signals supported it. */
function SkillCard({ skill }: { skill: Skill }) {
  return (
    <div className="skill-card">
      <div className="skill-card-title">{skill.name}</div>
      {skill.evidence && <p className="skill-card-why">{skill.evidence}</p>}
      <div className="skill-card-foot">
        <span>observed · {skill.projects.join(", ")}</span>
        {skill.signals.length > 0 && (
          <span className="skill-card-signals">
            {skill.signals.map((s) => SIGNAL_LABELS[s] ?? s).join(" · ")}
          </span>
        )}
      </div>
    </div>
  );
}

/** One segment of the comb: a category's teeth hanging from a shared spine, with the
 *  category name captioned ABOVE the spine. Segments sit side by side and align at the top
 *  so their spines form one line — the whole row reads as a single segmented comb (the
 *  comb-shaped-skills form) rather than isolated charts. */
function CombCluster({ category, skills }: { category: string; skills: Skill[] }) {
  return (
    <div className="comb-cluster">
      <div className="comb-cluster-label">{category}</div>
      <div className="comb" role="img" aria-label={`${category} skills by depth`}>
        {skills.map((s) => (
          <CombTooth key={s.name} skill={s} />
        ))}
      </div>
    </div>
  );
}

/** One category's evidence cards, under a section header — the provenance behind the teeth. */
function SkillCards({ category, skills }: { category: string; skills: Skill[] }) {
  return (
    <section className="skill-section">
      <div className="section-header">
        <h2>{category}</h2>
        <span className="section-rule" />
        <span className="section-aside">
          {skills.length} skill{skills.length === 1 ? "" : "s"}
        </span>
      </div>
      <div className="skill-grid">
        {skills.map((s) => (
          <SkillCard key={s.name} skill={s} />
        ))}
      </div>
    </section>
  );
}

export function Skills() {
  const { data, error, loading } = useApiData(getSkills, []);

  if (loading) return <div className="center-note">Loading…</div>;
  if (error) return <div className="center-note">Could not load skills.</div>;
  if (!data) return null;

  const groups = groupByCategory(data.skills, data.categories);

  return (
    <div>
      <div className="page-header">
        <div className="eyebrow">Observed from your work</div>
        <h1 className="page-title">Skills &amp; craft</h1>
        <p className="page-sub">
          The kinds of work and skills your projects demonstrate, read from real activity
          (languages, commits, and your own docs) and never authored here. The spine is your
          breadth across areas. Longer teeth hanging from it are skills your work shows more
          of, across more projects.
        </p>
      </div>

      {groups.length === 0 ? (
        <div className="center-note">
          {/* Wrap in a block so the inline mono span does not become a separate flex item
              of .center-note (which would collapse the surrounding spaces). */}
          <p className="center-note-text">
            No skills observed yet. Orion derives these from a project&rsquo;s activity once it
            has some, for any project that opts in with{" "}
            <span className="mono-inline">skills = true</span>.
          </p>
        </div>
      ) : (
        <>
          {/* The comb: a horizontal spine of category segments with teeth hanging down,
              tooth length varying by depth (the comb-shaped-skills form). */}
          <div className="comb-band">
            {groups.map((group) => (
              <CombCluster key={group.category} category={group.category} skills={group.skills} />
            ))}
          </div>
          {/* The provenance behind the teeth, grouped by category. */}
          {groups.map((group) => (
            <SkillCards key={group.category} category={group.category} skills={group.skills} />
          ))}
        </>
      )}
    </div>
  );
}
