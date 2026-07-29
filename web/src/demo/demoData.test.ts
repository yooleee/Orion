// =============================================================================
// web/src/demo/demoData.test.ts
// -----------------------------------------------------------------------------
// Responsible for: Pinning that the Showcase demo's fixtures are INTERNALLY COHERENT —
//                  every deadline agrees with the `state` recorded beside it (AU1-R F5).
// Role in project: The Showcase is Orion's one anonymous, internet-facing surface. In
//                  production the relay derives `state` from the date, so the two cannot
//                  disagree; these fixtures hardcode BOTH halves of that relationship, so
//                  nothing but a test can keep them honest. The demo previously rendered
//                  "◷ 26d overdue" in due-soon amber, because its dates were fixed calendar
//                  values that fell into the past while their states stayed frozen.
// Approach: read the dates straight out of the shipped fixtures and classify them with the
//           app's own daysUntil (not a reimplementation), then assert against the relay's
//           7-day due_soon rule.
// =============================================================================

import { describe, expect, it } from "vitest";
import { DEMO_PROJECT, DEMO_REPORTS } from "./demoData";
import { daysUntil } from "../lib/time";
import type { Status } from "../api/types";

// Mirrors relay/derive.py's DUE_SOON_DAYS, the rule the server classifies by.
const DUE_SOON_WINDOW_DAYS = 7;
const TZ = "America/Los_Angeles";

/** Every (state, due_date) pair the demo ships, from all three of its sources. */
function datedPairs(): { where: string; state: Status; dueDate: string }[] {
  const pairs: { where: string; state: Status; dueDate: string }[] = [];

  if (DEMO_PROJECT.stats.next_due) {
    pairs.push({
      where: "stats.next_due",
      state: DEMO_PROJECT.stats.next_due.state,
      dueDate: DEMO_PROJECT.stats.next_due.due_date,
    });
  }
  for (const row of DEMO_PROJECT.checklist) {
    if (row.due_date) {
      pairs.push({ where: `checklist "${row.text}"`, state: row.state, dueDate: row.due_date });
    }
  }
  // The report snapshots carry their own copies of the same items — the drift showed up here
  // too, so they are checked rather than assumed to follow the live checklist.
  for (const report of DEMO_REPORTS) {
    for (const row of report.checklist_snapshot?.rows ?? []) {
      if (row.due_date) {
        pairs.push({
          where: `report #${report.number} snapshot "${row.text}"`,
          state: row.state,
          dueDate: row.due_date,
        });
      }
    }
  }
  return pairs;
}

describe("demo fixture deadline coherence", () => {
  it("ships at least one dated item in each of its three sources", () => {
    // Guards the guard: if the fixtures were restructured so the walk above found nothing,
    // every assertion below would vacuously pass and the demo could rot unnoticed.
    const pairs = datedPairs();
    expect(pairs.length).toBeGreaterThanOrEqual(6);
    expect(pairs.some((p) => p.where === "stats.next_due")).toBe(true);
    expect(pairs.some((p) => p.where.startsWith("checklist"))).toBe(true);
    expect(pairs.some((p) => p.where.startsWith("report"))).toBe(true);
  });

  it("never shows a past deadline (the demo project is 'active')", () => {
    // DEMO_PROJECT.lifecycle is "active", which the fixture's own comment describes as "a
    // live NEXT DUE". A negative day count is what produced the "26d overdue" label.
    for (const { where, dueDate } of datedPairs()) {
      expect(daysUntil(dueDate, TZ), `${where} is in the past`).toBeGreaterThanOrEqual(0);
    }
  });

  it("keeps every state consistent with the relay's due_soon rule", () => {
    for (const { where, state, dueDate } of datedPairs()) {
      const days = daysUntil(dueDate, TZ);
      if (state === "due_soon") {
        expect(days, `${where} is due_soon but ${days}d away`).toBeLessThanOrEqual(
          DUE_SOON_WINDOW_DAYS,
        );
      }
      if (state === "upcoming") {
        expect(days, `${where} is upcoming but only ${days}d away`).toBeGreaterThan(
          DUE_SOON_WINDOW_DAYS,
        );
      }
      // "overdue" is deliberately not expected anywhere: an active demo should never show
      // one. If a fixture ever legitimately needs an overdue item, its date must be in the
      // past, which the previous test would then catch — forcing a deliberate decision
      // rather than letting the state and the date drift apart silently.
      expect(state, `${where} should not be overdue in the demo`).not.toBe("overdue");
    }
  });
});
