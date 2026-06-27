// =============================================================================
// web/src/components/ScheduleRow.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the Scheduling row's guarantees — the deadline state is
//                  legible without colour (glyph + a relative-time label), and the
//                  source tag renders the right glyph for a project (◇) vs a tracker
//                  (⊟) so you can always tell where a deadline comes from.
// =============================================================================

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScheduleRow } from "./ScheduleRow";
import { STATUS_STYLES, SOURCE_GLYPH } from "../theme/status";
import type { ScheduleItem } from "../api/types";

const TZ = "America/Los_Angeles";

function row(overrides: Partial<ScheduleItem>): ScheduleItem {
  return {
    state: "overdue",
    label: "Some deadline",
    due_date: "2000-01-01",
    slipping: false,
    source: { name: "orion", kind: "project" },
    ...overrides,
  };
}

describe("ScheduleRow — state legible without colour alone", () => {
  it("shows the overdue glyph + a relative-time label", () => {
    render(<ScheduleRow item={row({ state: "overdue", due_date: "2000-01-01" })} tz={TZ} />);
    expect(screen.getByText(STATUS_STYLES.overdue.glyph)).toBeInTheDocument(); // ▲
    expect(screen.getByText(/ago/)).toBeInTheDocument(); // a past deadline reads "Nd ago"
  });

  it("marks a slipping item with the slipping signal", () => {
    render(<ScheduleRow item={row({ slipping: true })} tz={TZ} />);
    expect(screen.getByText("slipping")).toBeInTheDocument();
  });
});

describe("ScheduleRow — source tag tells project from tracker", () => {
  it("renders ◇ + name for a project source", () => {
    render(<ScheduleRow item={row({ source: { name: "orion", kind: "project" } })} tz={TZ} />);
    expect(screen.getByText(SOURCE_GLYPH.project)).toBeInTheDocument(); // ◇
    expect(screen.getByText(/orion/)).toBeInTheDocument();
  });

  it("renders ⊟ + name for a tracker source", () => {
    render(
      <ScheduleRow item={row({ source: { name: "applications", kind: "tracker" } })} tz={TZ} />,
    );
    expect(screen.getByText(SOURCE_GLYPH.tracker)).toBeInTheDocument(); // ⊟
    expect(screen.getByText(/applications/)).toBeInTheDocument();
  });
});
