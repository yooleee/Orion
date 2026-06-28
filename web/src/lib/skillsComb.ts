// =============================================================================
// web/src/lib/skillsComb.ts
// -----------------------------------------------------------------------------
// Responsible for: The PURE layout math for the skills comb (E2 Inc 4 slice 4c) —
//                  mapping a skill's derived depth to a tooth LENGTH (how far the tooth
//                  hangs down from the spine), and grouping the flat skills list into
//                  ordered category buckets.
// Role in project: Kept OUT of Skills.tsx so the comb's geometry is unit-testable without
//                  rendering. The component stays declarative; this owns the arithmetic.
// Why a helper (not inline): the depth->height map and the grouping are the two bits with
//                  real logic worth pinning by test; everything else in Skills.tsx is markup.
// =============================================================================

import type { Skill } from "../api/types";

// Tooth length (px) per derived depth (1-4) — how far the tooth hangs DOWN from the spine.
// A comb's teeth vary in length to show depth; these are the four steps. Tuned by eyes-on
// (no committed screenshot for this section), so they live in one named place rather than
// scattered inline — easy to adjust.
export const COMB_HEIGHTS: Record<number, number> = { 1: 34, 2: 64, 3: 96, 4: 128 };

/**
 * Map a skill's derived depth to its tooth length in pixels (how far it descends).
 *
 * Args:
 *   depth: the server-derived ordinal depth (expected 1-4).
 *
 * Returns: the tooth length in px, clamped to the [1, 4] range so an out-of-range depth
 *   (a future wider scale, or bad data) still draws a sane tooth rather than nothing.
 *
 * Why: the wire ships a semantic depth (1-4), and presentation (how long the tooth is)
 *   lives client-side — the same server-decides-meaning / client-decides-looks split the
 *   rest of the dashboard uses (e.g. status glyphs). Clamping keeps the comb robust to
 *   scale changes. The name is kept (it is the bar's CSS height) so callers/tests are stable.
 */
export function toothHeight(depth: number): number {
  const clamped = Math.max(1, Math.min(4, Math.round(depth)));
  return COMB_HEIGHTS[clamped];
}

/** A category and the skills under it, in render order. */
export interface SkillGroup {
  category: string;
  skills: Skill[];
}

/**
 * Group the flat skills list into category buckets, in the given category order.
 *
 * Args:
 *   skills: the merged skills (already ordered by the server: category rank, then tallest
 *     tooth first, then name).
 *   categories: the category order from the server (strongest group first).
 *
 * Returns: one SkillGroup per category, each carrying that category's skills in their
 *   server order. A category with no skills is omitted (defensive — the server only lists
 *   categories that have skills, but this keeps the comb from drawing an empty group).
 *
 * Why: the comb renders one block per category, but the wire is a FLAT list (so the server
 *   can order across categories in one pass). Splitting here — rather than in the server —
 *   keeps the wire minimal and the grouping a presentation concern. The server's ordering
 *   is preserved because we filter (stable) rather than re-sort.
 */
export function groupByCategory(skills: Skill[], categories: string[]): SkillGroup[] {
  return categories
    .map((category) => ({
      category,
      skills: skills.filter((s) => s.category === category),
    }))
    .filter((group) => group.skills.length > 0);
}
