// =============================================================================
// web/src/lib/skillsComb.test.ts
// -----------------------------------------------------------------------------
// Responsible for: Pinning the comb's pure layout math — depth maps to a monotonic
//                  tooth height (clamped), and the flat skills list groups into the
//                  server's category order preserving each category's skill order.
// Why these: the helper is the only logic in the Skills section worth testing without
//            rendering; if depth->height is not monotonic the comb misleads, and if
//            grouping re-orders, the "strongest first" intent the server set is lost.
// =============================================================================

import { describe, expect, it } from "vitest";
import { toothHeight, groupByCategory } from "./skillsComb";
import type { Skill } from "../api/types";

function skill(name: string, category: string, depth = 1): Skill {
  return { name, category, depth, projects: ["orion"], evidence: "ev", signals: ["git"] };
}

describe("toothHeight", () => {
  it("is strictly increasing across the depth steps", () => {
    // A taller tooth must mean more depth — otherwise the comb misrepresents the work.
    expect(toothHeight(1)).toBeLessThan(toothHeight(2));
    expect(toothHeight(2)).toBeLessThan(toothHeight(3));
    expect(toothHeight(3)).toBeLessThan(toothHeight(4));
  });

  it("clamps out-of-range depths to the nearest valid tooth", () => {
    // Robust to bad data or a future wider scale: depth 0 / 7 still draw a sane tooth.
    expect(toothHeight(0)).toBe(toothHeight(1));
    expect(toothHeight(7)).toBe(toothHeight(4));
  });
});

describe("groupByCategory", () => {
  it("groups in the server's category order and preserves each group's skill order", () => {
    // The server orders categories strongest-first and skills tallest-first; grouping must
    // not reshuffle either, so the comb renders exactly the order the server decided.
    const skills = [
      skill("A", "Backend", 3),
      skill("B", "Backend", 1),
      skill("C", "ML / NLP", 2),
    ];
    const groups = groupByCategory(skills, ["Backend", "ML / NLP"]);
    expect(groups.map((g) => g.category)).toEqual(["Backend", "ML / NLP"]);
    expect(groups[0].skills.map((s) => s.name)).toEqual(["A", "B"]);
    expect(groups[1].skills.map((s) => s.name)).toEqual(["C"]);
  });

  it("omits a category with no skills", () => {
    // Defensive: an empty category would draw a header with an empty comb.
    const groups = groupByCategory([skill("A", "Backend")], ["Backend", "Frontend"]);
    expect(groups.map((g) => g.category)).toEqual(["Backend"]);
  });
});
