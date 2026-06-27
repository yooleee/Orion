// =============================================================================
// web/src/components/TrackerRow.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the tracker row's two load-bearing guarantees — state is
//                  legible without colour alone (glyph + label, via StatusSignal), and
//                  the displayed label is the CLEAN title (embedded status stripped).
// Why these scenarios: the in-progress arc is the gap-8 feature, so it must render a
//                  legible "in progress" pill; and the design shows clean titles, so a
//                  status-embedding item must not echo its "- In progress" suffix.
// =============================================================================

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TrackerRow } from "./TrackerRow";
import type { ChecklistItem } from "../api/types";

const TZ = "America/Los_Angeles";

/** Build a ChecklistItem wire object, overriding only the fields a test cares about. */
function item(overrides: Partial<ChecklistItem>): ChecklistItem {
  return {
    text: "Some item",
    done: false,
    due_date: null,
    key: null,
    group: null,
    state: "not_started",
    status: null,
    slipping: false,
    ...overrides,
  };
}

describe("TrackerRow — state legible without colour alone", () => {
  it("renders an 'in progress' pill (glyph + label) for an in-progress item (gap 8)", () => {
    // The gap-8 case: an open, undated item the producer marked in_progress. The circular
    // arc is decorative (aria-hidden), so the legible signal is the text pill.
    render(<TrackerRow item={item({ status: "in_progress", state: "in_progress" })} tz={TZ} />);
    expect(screen.getByText("in progress")).toBeInTheDocument();
    expect(screen.getByText("◐")).toBeInTheDocument(); // the in-progress glyph
  });

  it("renders a 'not started' pill for an open, statusless item", () => {
    render(<TrackerRow item={item({})} tz={TZ} />);
    expect(screen.getByText("not started")).toBeInTheDocument();
  });

  it("labels a finished item by HOW it finished (submitted), not a redundant 'done'", () => {
    render(<TrackerRow item={item({ done: true, status: "submitted", state: "done" })} tz={TZ} />);
    expect(screen.getByText("submitted")).toBeInTheDocument();
    expect(screen.queryByText("done")).not.toBeInTheDocument();
  });

  it("shows a deadline chip alongside the status for an overdue open item", () => {
    // Hack Your Summer in the design: "not started" + "Nd overdue" as two distinct signals.
    render(
      <TrackerRow item={item({ status: "not_started", state: "overdue", due_date: "2000-01-01" })} tz={TZ} />,
    );
    expect(screen.getByText("not started")).toBeInTheDocument();
    expect(screen.getByText(/overdue/)).toBeInTheDocument();
  });
});

describe("TrackerRow — clean label (embedded status stripped)", () => {
  it("shows the bare `key` title, not the status-embedding `text`", () => {
    // The wire ships text with the status baked in; `key` is the clean title. The row must
    // display the clean title so the design's "Claude Corps Fellow (job)" reads without
    // its "- Not started" suffix.
    render(
      <TrackerRow
        item={item({
          text: "Claude Corps Fellow (job) - Not started",
          key: "Claude Corps Fellow (job)",
          status: "not_started",
        })}
        tz={TZ}
      />,
    );
    expect(screen.getByText("Claude Corps Fellow (job)")).toBeInTheDocument();
    expect(screen.queryByText(/- Not started/)).not.toBeInTheDocument();
  });

  it("falls back to `text` when there is no key (a status-less table row)", () => {
    render(<TrackerRow item={item({ text: "Review GitHub repo", key: null })} tz={TZ} />);
    expect(screen.getByText("Review GitHub repo")).toBeInTheDocument();
  });
});
