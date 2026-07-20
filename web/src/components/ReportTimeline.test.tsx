// =============================================================================
// web/src/components/ReportTimeline.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the C3 Inc 2 report-attribution display on the timeline —
//                  an attributed report shows its producer's name, and a legacy/older
//                  report (author_name null) renders exactly as before (no author text).
// Role in project: Guards the "old rows stay pixel-identical" promise: the author is
//                  additive and appears only when present.
// =============================================================================

import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ReportTimeline } from "./ReportTimeline";
import type { ReportSummary } from "../api/types";

// A minimal ReportSummary; each test overrides only author_name (the field under test).
function summary(over: Partial<ReportSummary>): ReportSummary {
  return {
    id: 1,
    number: 1,
    title: "Auth landed",
    generated_at: "2026-06-28T10:00:00+00:00",
    lane: "structured",
    share_level: "high_level",
    section_count: 3,
    author_name: null,
    author_kind: null,
    operated_by_name: null,
    source_tags: [],
    ...over,
  };
}

// ReportTimeline renders <Link>, so it needs a router context to render at all.
function renderTimeline(reports: ReportSummary[]) {
  return render(
    <MemoryRouter>
      <ReportTimeline reports={reports} />
    </MemoryRouter>,
  );
}

describe("ReportTimeline — report attribution", () => {
  it("shows the producer's name on an attributed report", () => {
    const { getByText } = renderTimeline([summary({ author_name: "Teammate B" })]);
    // The name appears in the meta line (alongside the ordinal + relative time).
    expect(getByText(/Teammate B/)).toBeInTheDocument();
  });

  it("renders no author text for a legacy/older report (author_name null)", () => {
    const { container } = renderTimeline([summary({ id: 2, author_name: null })]);
    const meta = container.querySelector(".timeline-meta");
    // The meta line still renders, but carries no author separator/name.
    expect(meta).not.toBeNull();
    expect(meta?.textContent).not.toContain("·  ·"); // no empty author segment
    expect(container.textContent).not.toContain("undefined");
    expect(container.textContent).not.toContain("null");
  });

  // --- Unit 4a: agent attribution ------------------------------------------
  // An agent pushes on a human's behalf, so the timeline must show BOTH facts: the
  // agent keeps the attribution (provenance is never lost) and the operator is named.

  it("badges an agent's report and names the human it acted for", () => {
    const { container, getByText } = renderTimeline([
      summary({ author_name: "claude-mac", author_kind: "agent", operated_by_name: "Supervisor A" }),
    ]);
    expect(container.querySelector(".agent-chip")?.textContent).toBe("agent");
    expect(getByText(/claude-mac/)).toBeInTheDocument(); // the agent keeps the attribution
    expect(getByText(/operated by Supervisor A/)).toBeInTheDocument();
  });

  it("shows no chip for a human's report", () => {
    // The overwhelmingly common case must stay visually unchanged — badging every
    // human push would be noise, so only "agent" renders a chip.
    const { container } = renderTimeline([
      summary({ author_name: "Teammate B", author_kind: "human", operated_by_name: null }),
    ]);
    expect(container.querySelector(".agent-chip")).toBeNull();
    expect(container.textContent).not.toContain("operated by");
  });

  it("badges an agent whose operator account was deleted, without naming one", () => {
    // The read-time attribution join returns a null operator once that account is gone.
    // The agent is still legitimately an agent, so the chip stays — we just have no
    // human left to name, and must not render "operated by null".
    const { container } = renderTimeline([
      summary({ author_name: "claude-mac", author_kind: "agent", operated_by_name: null }),
    ]);
    expect(container.querySelector(".agent-chip")).not.toBeNull();
    expect(container.textContent).not.toContain("operated by");
    expect(container.textContent).not.toContain("null");
  });
});
