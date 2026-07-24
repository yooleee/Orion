// =============================================================================
// web/src/components/PastProjects.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning Home's collapsed "Past projects" section (S2.2) — that it is
//                  collapsed by default, expands to BOTH kinds, counts what it hides, and
//                  disappears entirely when nothing is past.
// Why: this section is the visible half of "a finished project stops competing for
//      attention". If it defaulted to open it would just re-add the density it exists to
//      remove, and if it dropped trackers a past tracker would sit unmarked among the live
//      ones — the exact inconsistency the both-kinds decision was made to avoid.
// =============================================================================

import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { PastProjects } from "./PastProjects";
import type { ProjectSummary, TrackerSummary } from "../api/types";

/** A finished project row, already urgency-stripped the way the relay sends one. */
function pastProject(name: string): ProjectSummary {
  return {
    name,
    kind: "project",
    lifecycle: "past",
    headline: "Wrapped up in June.",
    progress: { done: 12, total: 12, pct: 100 },
    at_risk: 0,
    slipping: 0,
    next_due: null,
    updated_at: "2026-06-01T10:00:00+00:00",
    report_id: 9,
    about: null,
  };
}

function pastTracker(name: string): TrackerSummary {
  return {
    name,
    kind: "tracker",
    lifecycle: "past",
    item_count: 4,
    progress: { done: 4, total: 4, pct: 100 },
    // Urgent slices already folded into `remaining` by the relay.
    segments: { overdue: 0, due_soon: 0, remaining: 0, done: 4 },
    at_risk: 0,
    slipping: 0,
    next_due: null,
    at_risk_items: [],
    updated_at: "2026-06-01T10:00:00+00:00",
  };
}

function renderSection(projects: ProjectSummary[], trackers: TrackerSummary[]) {
  return render(
    <MemoryRouter>
      <PastProjects projects={projects} trackers={trackers} tz="UTC" />
    </MemoryRouter>,
  );
}

describe("PastProjects — Home's collapsed finished-work section", () => {
  it("is collapsed by default, showing only the heading and a count", () => {
    renderSection([pastProject("old-one"), pastProject("old-two")], []);

    expect(screen.getByText("Past projects")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    // The rows themselves are not mounted until asked for — that is the density fix.
    expect(screen.queryByText("old-one")).toBeNull();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("expands to reveal past projects AND past trackers together", () => {
    renderSection([pastProject("old-project")], [pastTracker("old-tracker")]);

    // The count covers both kinds — nothing marked past is left out of the tally.
    expect(screen.getByText("2")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Past projects/ }));

    expect(screen.getByText("old-project")).toBeInTheDocument();
    expect(screen.getByText("old-tracker")).toBeInTheDocument();
  });

  it("renders nothing at all when no project is past", () => {
    const { container } = renderSection([], []);
    // Not an empty "Past projects (0)" — a permanent reminder of an absence is worse than
    // saying nothing.
    expect(container).toBeEmptyDOMElement();
  });

  it("shows no forward signal on a past project row", () => {
    // A past row arrives with at_risk 0 and next_due null, which would otherwise fall
    // through to "✓ on track" — a claim about work still in flight. A finished project is
    // neither on track nor off it.
    renderSection([pastProject("old-one")], []);
    fireEvent.click(screen.getByRole("button", { name: /Past projects/ }));

    expect(screen.getByText("old-one")).toBeInTheDocument();
    expect(screen.queryByText("on track")).toBeNull();
  });
});
