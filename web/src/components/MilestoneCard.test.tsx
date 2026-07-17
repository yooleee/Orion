// =============================================================================
// web/src/components/MilestoneCard.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the milestone slipping-count label rule (E1.2 Unit 5) —
//                  the card shows "N slipped" only when MORE THAN ONE item slips;
//                  a single slip stays the bare "slipped", and zero shows no slip
//                  signal at all.
// Role in project: The count is the whole visible payoff of Unit 5. The >1 threshold
//                  is real UI logic (chosen so the common single-slip case renders
//                  exactly as before), so it is enforced here rather than left to
//                  eyeballing across the three themes.
// =============================================================================

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MilestoneCard } from "./MilestoneCard";
import type { Milestone } from "../api/types";

// A minimal on-track milestone; each test overrides only the slipping fields it cares
// about. at_risk = 0 and nearest_due = null keep other status signals out of the way so
// the assertions isolate the slip label.
function milestone(overrides: Partial<Milestone>): Milestone {
  return {
    group: "Auth",
    done: 1,
    total: 3,
    at_risk: 0,
    nearest_due: null,
    slipping: false,
    slipping_count: 0,
    ...overrides,
  };
}

describe("MilestoneCard — slipping count label", () => {
  it("shows the count when more than one item slips", () => {
    render(<MilestoneCard milestone={milestone({ slipping: true, slipping_count: 2 })} tz="UTC" />);
    expect(screen.getByText("2 slipped")).toBeInTheDocument();
  });

  it("shows the bare 'slipped' for a single slip (count omitted)", () => {
    render(<MilestoneCard milestone={milestone({ slipping: true, slipping_count: 1 })} tz="UTC" />);
    // The label is exactly "slipped" — NOT "1 slipped" — so the common case is unchanged.
    expect(screen.getByText("slipped")).toBeInTheDocument();
    expect(screen.queryByText("1 slipped")).not.toBeInTheDocument();
  });

  it("shows no slip signal when nothing slips", () => {
    render(<MilestoneCard milestone={milestone({ slipping: false, slipping_count: 0 })} tz="UTC" />);
    expect(screen.queryByText("slipped")).not.toBeInTheDocument();
  });
});
