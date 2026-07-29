// =============================================================================
// web/src/routes/Home.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning that Home renders three DISTINCT states for the portfolio —
//                  loading, failed-to-load, and loaded — rather than collapsing the first
//                  two into one spinner (AU1-R F5).
// Role in project: Home is the dashboard's landing surface and the only consumer of the
//                  shell's `portfolio`. Before this, Shell's fetch .catch wrote the initial
//                  null back, so a relay outage rendered "Loading…" forever with no error
//                  and no retry. These tests are the guard against that collapse returning.
// Approach: Home reads useOutletContext<ShellContext>(), so each case renders it under a
//           Route + Outlet wrapper supplying a hand-built context — no API mock is needed,
//           because the fetch lives in Shell and Home only reads the result.
// =============================================================================

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { Home } from "./Home";
import type { Me, Portfolio } from "../api/types";
import type { ShellContext } from "../components/Shell";

const ME: Me = {
  gated: false,
  authenticated: true,
  identity: { name: "root", role: "admin" },
  scope: { unrestricted: true, projects: null },
  display_tz: "America/Los_Angeles",
  showcase_enabled: false,
};

const EMPTY_PORTFOLIO: Portfolio = {
  scope: { unrestricted: true, projects: null },
  projects: [],
  trackers: [],
};

/**
 * Render Home with an explicit shell context.
 *
 * Args:
 *   portfolio: the loaded portfolio, or null when absent (loading or failed).
 *   portfolioError: why it is absent, or null. This is the argument under test — it is
 *     what separates "failed" from "still loading", since `portfolio` is null for both.
 *
 * Why: Home is a pure consumer of the Outlet context, so driving the context directly
 * tests exactly the branch logic without standing up Shell's effect and fetch.
 */
function renderHome(portfolio: Portfolio | null, portfolioError: Error | null) {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route element={<Outlet context={{ me: ME, portfolio, portfolioError } satisfies ShellContext} />}>
          <Route path="/" element={<Home />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Home portfolio states", () => {
  it("shows a loading note while the portfolio is in flight", () => {
    // The genuinely-still-loading case: nothing has come back and nothing has failed.
    renderHome(null, null);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows an error note, not a spinner, when the portfolio failed to load", () => {
    // The case this unit exists for. `portfolio` is null here exactly as in the loading
    // case, so if the two states were ever collapsed again this assertion is what fails.
    renderHome(null, new Error("relay unreachable"));
    expect(screen.getByText(/Could not load your portfolio/)).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("renders the portfolio once loaded, with neither note", () => {
    // The success path must not be affected by the new error branch. An empty portfolio is
    // used because it is a real, reachable state (a fresh relay) and still proves Home got
    // past both guards into its own render.
    renderHome(EMPTY_PORTFOLIO, null);
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
    expect(screen.queryByText(/Could not load your portfolio/)).not.toBeInTheDocument();
    expect(screen.getByText("Portfolio overview")).toBeInTheDocument();
  });
});
