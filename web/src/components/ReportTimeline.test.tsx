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
});
