// =============================================================================
// web/src/components/DisciplineCard.tsx
// -----------------------------------------------------------------------------
// Responsible for: One observed-principle card — a bold title, a "why" paragraph, and
//                  an "observed · <source>" footer naming the doc it was read from.
// Role in project: The atom of the project page's "Working agreements" section (Unit 5)
//                  and the public Showcase demo's copy of it. Lifted out of the retired
//                  standalone Disciplines route so both surfaces share ONE renderer.
// Security: title / why / source are observed text from the API (untrusted) and render as
//           React text children — inert by construction (never dangerouslySetInnerHTML).
//           A guarantee-test pins this.
// =============================================================================

import type { Discipline } from "../api/types";

/**
 * Render one principle card.
 *
 * Args:
 *   card: a Discipline ({title, why, source}) — the bold title, the "why" paragraph, and
 *     the repo-relative doc the "observed · <source>" footer names.
 *
 * Returns: the card element (no fetching, no state).
 *
 * Why: shared between the project page and the demo so the card looks and behaves the same
 * on both, and the inert-text security guarantee lives in exactly one place.
 */
export function DisciplineCard({ card }: { card: Discipline }) {
  return (
    <div className="disc-card">
      <div className="disc-card-title">{card.title}</div>
      <p className="disc-card-why">{card.why}</p>
      <div className="disc-card-foot">observed · {card.source}</div>
    </div>
  );
}
