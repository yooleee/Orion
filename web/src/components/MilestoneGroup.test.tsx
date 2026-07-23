// =============================================================================
// web/src/components/MilestoneGroup.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the KI-34 regroup's load-bearing behaviours — a group is
//                  COLLAPSED by default (that collapse is the density fix), expands to
//                  reveal exactly its own items, and the ungrouped "Other" case rolls up
//                  its counts from the items themselves without inventing a status.
// Role in project: The flat "Live checklist" section retired in favour of this component,
//                  so if expansion broke, a project's items would be unreachable on the
//                  page — the one regression that would make the regroup a net loss.
// =============================================================================

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MilestoneGroup } from "./MilestoneGroup";
import type { ChecklistItem, Milestone } from "../api/types";

function milestone(overrides: Partial<Milestone> = {}): Milestone {
  return {
    group: "Auth",
    done: 1,
    total: 2,
    at_risk: 0,
    nearest_due: null,
    slipping: false,
    slipping_count: 0,
    ...overrides,
  };
}

function item(text: string, overrides: Partial<ChecklistItem> = {}): ChecklistItem {
  return {
    text,
    done: false,
    due_date: null,
    key: text,
    group: "Auth",
    state: "not_started",
    status: null,
    slipping: false,
    ...overrides,
  };
}

describe("MilestoneGroup — expandable regroup (KI-34)", () => {
  it("is collapsed by default: the roll-up shows, the items do not", () => {
    render(
      <MilestoneGroup
        title="Auth"
        milestone={milestone()}
        items={[item("Password reset"), item("OAuth", { done: true })]}
        tz="UTC"
      />,
    );
    // The summary is always visible (title + counts) — this is the scannable layer.
    expect(screen.getByText("Auth")).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
    // The items are NOT rendered until expanded — collapsing is the density fix itself.
    expect(screen.queryByText("Password reset")).toBeNull();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("reveals exactly its own items when expanded, and collapses again", () => {
    render(
      <MilestoneGroup
        title="Auth"
        milestone={milestone()}
        items={[item("Password reset"), item("OAuth", { done: true })]}
        tz="UTC"
      />,
    );
    const toggle = screen.getByRole("button");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Password reset")).toBeInTheDocument();
    expect(screen.getByText("OAuth")).toBeInTheDocument();

    // Toggling back hides them again (the disclosure is genuinely two-way).
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Password reset")).toBeNull();
  });

  it("shows the milestone's status signal in the summary", () => {
    render(
      <MilestoneGroup
        title="Auth"
        milestone={milestone({ slipping: true, slipping_count: 2 })}
        items={[item("Password reset")]}
        tz="UTC"
      />,
    );
    // Composed by the SHARED milestoneSignals rule, so the group and the card agree.
    expect(screen.getByText("2 slipped")).toBeInTheDocument();
  });

  it("counts an ungrouped 'Other' group from its items and shows no invented status", () => {
    const { container } = render(
      <MilestoneGroup
        title="Other"
        milestone={null}
        items={[
          item("Loose one", { group: null, done: true }),
          item("Loose two", { group: null }),
          item("Loose three", { group: null }),
        ]}
        tz="UTC"
      />,
    );
    // done/total derive from the items themselves (no server-side milestone here).
    expect(screen.getByText("1/3")).toBeInTheDocument();
    // No status signal is fabricated for a bucket the server never derived a state for.
    expect(container.querySelector(".status-signal")).toBeNull();
    // It still expands — ungrouped items must remain reachable now the flat list is gone.
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("Loose two")).toBeInTheDocument();
  });
});
