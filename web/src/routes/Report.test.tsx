// =============================================================================
// web/src/routes/Report.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the C3 Inc 2 "pushed by" line on the single-report page —
//                  it shows the producer for an attributed report and is absent for a
//                  legacy/older report (author_name null), so old reports read unchanged.
// Approach: mock the API client (no network); keep the real ApiError (the component's
//           error branch uses it); render under MemoryRouter (matches the other route tests).
// =============================================================================

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { Report } from "./Report";
import { getReport } from "../api/client";
import type { ReportDetail } from "../api/types";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, getReport: vi.fn() };
});
const mockGet = vi.mocked(getReport);

beforeEach(() => mockGet.mockReset());

// A minimal ReportDetail; each test overrides only author_name (the field under test).
function detail(over: Partial<ReportDetail>): ReportDetail {
  return {
    id: 1,
    number: 1,
    project: "demo",
    title: "Auth landed",
    sections: [["SHIPPED", "Auth is done."]],
    body: "Auth landed. Auth is done.",
    lane: "structured",
    share_level: "high_level",
    generated_at: "2026-06-28T10:00:00+00:00",
    ingested_at: "2026-06-28T10:01:00+00:00",
    orion_version: "0.1.0",
    author_name: null,
    participants: [],
    source_tags: [],
    checklist_snapshot: { done: 0, total: 0, rows: [] },
    nav: { prev_id: null, prev_number: null, next_id: null, next_number: null },
    ...over,
  };
}

function renderReport() {
  return render(
    <MemoryRouter>
      <Report />
    </MemoryRouter>,
  );
}

describe("Report — pushed-by attribution", () => {
  it("shows 'pushed by <name>' on an attributed report", async () => {
    mockGet.mockResolvedValue(detail({ author_name: "Teammate B" }));
    renderReport();
    expect(await screen.findByText(/pushed by Teammate B/)).toBeInTheDocument();
  });

  it("shows no 'pushed by' for a legacy/older report (author_name null)", async () => {
    mockGet.mockResolvedValue(detail({ author_name: null }));
    renderReport();
    // Wait for the header to render, then assert the attribution phrase is absent.
    expect(await screen.findByText(/Progress report #1/)).toBeInTheDocument();
    expect(screen.queryByText(/pushed by/)).toBeNull();
  });
});
