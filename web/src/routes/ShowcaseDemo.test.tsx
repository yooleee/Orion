// =============================================================================
// web/src/routes/ShowcaseDemo.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the public demo walkthrough's two load-bearing properties —
//                  (1) it renders the fabricated sample project entirely from fixtures and
//                  makes NO network call (the security guarantee: no backend access, so no
//                  real data can leak), and (2) tab + report navigation stays inside the
//                  demo (never enters an authed route) and the surface is read-only.
// Approach: replace globalThis.fetch with a spy and assert it is never called across a
//           full click-through; render under ThemeProvider + MemoryRouter (the top bar's
//           theme switcher + "← Showcase" link need both).
// =============================================================================

import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ShowcaseDemo } from "./ShowcaseDemo";
import { ThemeProvider } from "../theme/ThemeProvider";

// Stand in for the network so any accidental fetch is caught (and fails loudly).
const fetchMock = vi.fn(() => Promise.reject(new Error("the demo must not hit the network")));
let originalFetch: typeof globalThis.fetch;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;
  fetchMock.mockClear();
});
afterEach(() => {
  globalThis.fetch = originalFetch;
});

function renderDemo() {
  return render(
    <ThemeProvider>
      <MemoryRouter>
        <ShowcaseDemo />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("ShowcaseDemo — fabricated, read-only, no network", () => {
  it("renders the sample project from fixtures and never calls fetch", () => {
    renderDemo();

    // Overview content comes straight from the fixtures.
    expect(screen.getByRole("heading", { name: "sample-app", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Google OAuth login")).toBeInTheDocument();

    // The discussion thread shows fabricated items but NO composer (read-only for a guest).
    expect(
      screen.getByText("How's the OAuth piece coming? Anything blocking the launch milestone?"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Posting as/)).not.toBeInTheDocument();

    // The whole point: the demo is static frontend data — a real-data leak is impossible
    // because there is no backend call at all.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("navigates tabs and into a report without leaving the demo or hitting the network", () => {
    renderDemo();

    fireEvent.click(screen.getByRole("button", { name: "Skills" }));
    // The skill name appears in both the comb tooth and its evidence card.
    expect(screen.getAllByText("React & TypeScript").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Disciplines" }));
    expect(screen.getByText("Small, reviewable changes")).toBeInTheDocument();

    // Back to Overview, then open the newest report and confirm its detail renders.
    fireEvent.click(screen.getByRole("button", { name: "Overview" }));
    fireEvent.click(screen.getByText("OAuth underway; search shipped"));
    expect(screen.getByText(/Progress report #3/)).toBeInTheDocument();

    expect(fetchMock).not.toHaveBeenCalled();
  });
});
