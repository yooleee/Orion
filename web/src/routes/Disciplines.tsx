// =============================================================================
// web/src/routes/Disciplines.tsx
// -----------------------------------------------------------------------------
// Responsible for: The Disciplines & directions page — the working principles Orion
//                  OBSERVED in the user's own docs, split into a Global group
//                  (conventions across all projects) and per-project groups. Each card
//                  is a bold title, a "why" paragraph, and an "observed · <source>" footer.
// Role in project: The "how the work is built and why" lens, reinforcing observe-not-
//                  originate (E2 Inc 4 slice 4b). Reads /api/disciplines (server already
//                  scope-filters + does the Global/project split). Visual oracle:
//                  design/screenshots/desktop-06-disciplines-sepia.png.
// Security: title / why / source are observed text from the API (untrusted) and render as
//           React text children — inert by construction (never dangerouslySetInnerHTML).
//           A guarantee-test pins this.
// =============================================================================

import type { Discipline, DisciplinesData } from "../api/types";
import { getDisciplines } from "../api/client";
import { useApiData } from "../lib/useApiData";

/** One principle card: bold title, the "why" paragraph, and the observed-source footer. */
function DisciplineCard({ card }: { card: Discipline }) {
  return (
    <div className="disc-card">
      <div className="disc-card-title">{card.title}</div>
      <p className="disc-card-why">{card.why}</p>
      <div className="disc-card-foot">observed · {card.source}</div>
    </div>
  );
}

/** A titled group (Global, or a project) with a right-aligned mono aside and a 2-col grid. */
function DisciplineGroupSection({
  title,
  aside,
  cards,
}: {
  title: string;
  aside: string;
  cards: Discipline[];
}) {
  return (
    <section className="disc-section">
      <div className="section-header">
        <h2>{title}</h2>
        <span className="section-rule" />
        <span className="section-aside">{aside}</span>
      </div>
      <div className="disc-grid">
        {cards.map((card, i) => (
          <DisciplineCard key={`${card.title}-${i}`} card={card} />
        ))}
      </div>
    </section>
  );
}

/**
 * The presentational Disciplines page, given already-loaded data.
 *
 * Args:
 *   data: a DisciplinesData (the Global group + per-project groups).
 *
 * Returns: the rendered page (no fetching).
 *
 * Why: separating render from fetch lets the public Showcase demo render this exact page
 * from fabricated fixtures (no API call), while the route below keeps fetching live data.
 */
export function DisciplinesView({ data }: { data: DisciplinesData }) {
  const isEmpty = data.global.length === 0 && data.projects.length === 0;

  return (
    <div>
      <div className="page-header">
        <div className="eyebrow">Observed conventions</div>
        <h1 className="page-title">Disciplines &amp; directions</h1>
        <p className="page-sub">
          How the work is built and why — read from your instruction &amp; design documents,
          never authored here. Meant to make the work legible to an outside reader.
        </p>
      </div>

      {isEmpty ? (
        <div className="center-note">
          No disciplines observed yet — Orion reflects the principles stated in your docs once
          they&rsquo;re collected.
        </div>
      ) : (
        <>
          {data.global.length > 0 && (
            <DisciplineGroupSection
              title="Global"
              aside="across all projects"
              cards={data.global}
            />
          )}
          {data.projects.map((group) => (
            <DisciplineGroupSection
              key={group.name}
              title={group.name}
              aside="project-specific"
              cards={group.principles}
            />
          ))}
        </>
      )}
    </div>
  );
}

export function Disciplines() {
  const { data, error, loading } = useApiData(getDisciplines, []);

  if (loading) return <div className="center-note">Loading…</div>;
  if (error) return <div className="center-note">Could not load disciplines.</div>;
  if (!data) return null;

  return <DisciplinesView data={data} />;
}
