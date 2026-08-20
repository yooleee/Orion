// =============================================================================
// web/src/routes/Search.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the Search page's states and the shape of its results —
//                  prompt / searching / error / no-matches stay DISTINCT, hits group
//                  by project with the two classes separate, links target the report
//                  page / the project's discussion anchor, and a capped class says so.
// Approach: mock getSearch (the client getter, à la Project.test.tsx); drive the query
//           through the router's ?q= (the page's single source of query state).
// =============================================================================

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { Search } from "./Search";
import { getSearch } from "../api/client";
import type { SearchResults } from "../api/types";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, getSearch: vi.fn() };
});
const mockSearch = vi.mocked(getSearch);

// Braces matter: mockReset() RETURNS the mock (chainable), and vitest calls a function
// returned from beforeEach as a cleanup hook — which would invoke the spy after the
// test and leak an unhandled rejection in the rejected-mock case.
beforeEach(() => {
  mockSearch.mockReset();
});

/** Build a full SearchResults with overridable classes (defaults: empty, uncapped). */
function results(partial: Partial<SearchResults> = {}): SearchResults {
  return {
    query: "seam",
    reports: { hits: [], capped: false },
    discussions: { hits: [], capped: false },
    ...partial,
  };
}

function renderSearch(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/search" element={<Search />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Search page states", () => {
  it("shows the prompt (and never calls the API) when the query is absent or short", () => {
    // The client-side mirror of the server's 2-char 400: below the minimum there is
    // nothing to ask, so no fetch happens and the prompt renders instead of an error.
    renderSearch("/search");
    expect(screen.getByText(/Type at least two characters/)).toBeInTheDocument();
    renderSearch("/search?q=x");
    expect(screen.getAllByText(/Type at least two characters/).length).toBeGreaterThan(0);
    expect(mockSearch).not.toHaveBeenCalled();
  });

  it("shows a distinct error note when the search call fails", async () => {
    // Error and "no matches" must never collapse: a relay outage is not an empty result.
    mockSearch.mockRejectedValue(new Error("down"));
    renderSearch("/search?q=seam");
    expect(await screen.findByText(/Could not search/)).toBeInTheDocument();
  });

  it("says no matches (echoing the query) when both classes come back empty", async () => {
    mockSearch.mockResolvedValue(results());
    renderSearch("/search?q=seam");
    expect(await screen.findByText(/No matches for “seam”/)).toBeInTheDocument();
  });
});

describe("Search results rendering", () => {
  it("groups hits by project and keeps the two classes distinct within a group", async () => {
    // Two projects, mixed classes: each project renders ONE group; within "orion" the
    // report and discussion hits sit under separate class labels (the wire keeps the
    // classes apart; so must the page).
    mockSearch.mockResolvedValue(
      results({
        reports: {
          hits: [
            { id: 41, project: "orion", title: "Auth revamp closed", generated_at: "2026-07-20T18:00:00+00:00", snippet: "the auth seam landed" },
            { id: 12, project: "sar_hackathon", title: "Seam day", generated_at: "2026-06-21T18:00:00+00:00", snippet: "hackathon seam work" },
          ],
          capped: false,
        },
        discussions: {
          hits: [
            { id: 7, project: "orion", author_name: "Supervisor A", role: "supervisor", created_at: "2026-07-21T09:00:00+00:00", snippet: "is the seam done?" },
          ],
          capped: false,
        },
      }),
    );
    renderSearch("/search?q=seam");

    // Both project groups appear, and the class labels are present in orion's group.
    expect(await screen.findByText("orion")).toBeInTheDocument();
    expect(screen.getByText("sar_hackathon")).toBeInTheDocument();
    // "Reports" appears once per project group that has report hits (both here);
    // "Discussion" only under orion, whose group holds the one discussion hit.
    expect(screen.getAllByText("Reports")).toHaveLength(2);
    expect(screen.getAllByText("Discussion")).toHaveLength(1);
    expect(screen.getByText("Supervisor A")).toBeInTheDocument();
  });

  it("links a report hit to its report page and a discussion hit to the project's discussion anchor", async () => {
    mockSearch.mockResolvedValue(
      results({
        reports: {
          hits: [{ id: 41, project: "orion", title: "Auth revamp closed", generated_at: "2026-07-20T18:00:00+00:00", snippet: "s" }],
          capped: false,
        },
        discussions: {
          hits: [{ id: 7, project: "orion", author_name: "Supervisor A", role: "supervisor", created_at: "2026-07-21T09:00:00+00:00", snippet: "s" }],
          capped: false,
        },
      }),
    );
    renderSearch("/search?q=seam");

    const reportLink = (await screen.findByText("Auth revamp closed")).closest("a");
    expect(reportLink).toHaveAttribute("href", "/report/41");
    const discussionLink = screen.getByText("Supervisor A").closest("a");
    expect(discussionLink).toHaveAttribute("href", "/project/orion#discussion");
  });

  it("renders a markup-shaped snippet inert (the escape-before-highlight pin, at page level)", async () => {
    // Belt over highlight.test.tsx's braces: the page passes RAW server text into the
    // highlighter; nothing on the way may ever interpret it as HTML.
    const nasty = "beware <script>alert(1)</script> here";
    mockSearch.mockResolvedValue(
      results({
        reports: {
          hits: [{ id: 1, project: "orion", title: "t", generated_at: "2026-07-20T18:00:00+00:00", snippet: nasty }],
          capped: false,
        },
      }),
    );
    const { container } = renderSearch("/search?q=script");
    await screen.findByText("orion");
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain(nasty);
  });

  it("shows a per-class capped note only for the class that was capped", async () => {
    // The cap must never be silent — and it must not overclaim either: only the capped
    // class gets the note.
    mockSearch.mockResolvedValue(
      results({
        reports: {
          hits: [{ id: 1, project: "orion", title: "t", generated_at: "2026-07-20T18:00:00+00:00", snippet: "seam" }],
          capped: true,
        },
      }),
    );
    renderSearch("/search?q=seam");
    expect(await screen.findByText(/newest 50 report matches/)).toBeInTheDocument();
    expect(screen.queryByText(/newest 50 discussion matches/)).not.toBeInTheDocument();
  });
});
