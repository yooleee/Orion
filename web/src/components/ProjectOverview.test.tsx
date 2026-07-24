// =============================================================================
// web/src/components/ProjectOverview.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the shared project overview extracted in DR1-R U1 — the one
//                  component both the real project page and the public Showcase demo now
//                  render. The load-bearing properties are (1) the About band shows the
//                  observed About line, (2) the forward look is expandable milestone GROUPS
//                  (no flat checklist), (3) the link-mode seam: "app" links reports into the
//                  authed /report/:id route, "none" opens them via onOpenReport with NO
//                  router at all (the demo path), and (4) the caller's discussion slot lands
//                  in the right column.
// Why: this component is shared by two routes, so a drift or a broken link mode would hit
//      both the real dashboard and the public demo at once — exactly the coupling U1 traded
//      duplication for. These tests are the guard on that trade.
// =============================================================================

import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ProjectOverview } from "./ProjectOverview";
import { DEMO_PROJECT } from "../demo/demoData";
import type { ProjectDetail } from "../api/types";

// A typed base project; each test spreads only the fields it cares about. DEMO_PROJECT is a
// full, valid ProjectDetail already, so reusing it keeps the fixture honest and DRY.
function project(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
  return { ...DEMO_PROJECT, ...overrides };
}

describe("ProjectOverview — shared project page body", () => {
  it("shows the About band from the observed About line", () => {
    render(
      <MemoryRouter>
        <ProjectOverview data={project({ about: "A tiny observed blurb." })} tz="UTC" />
      </MemoryRouter>,
    );
    expect(screen.getByText("A tiny observed blurb.")).toBeInTheDocument();
  });

  it("renders the forward look as collapsed milestone groups, not a flat checklist", () => {
    render(
      <MemoryRouter>
        <ProjectOverview data={project()} tz="UTC" />
      </MemoryRouter>,
    );
    // Group summaries are the scannable layer; there is no separate "Live checklist" section.
    expect(screen.getByText("Forward look")).toBeInTheDocument();
    expect(screen.getByText("Accounts & auth")).toBeInTheDocument();
    expect(screen.queryByText("Live checklist")).not.toBeInTheDocument();
    // A checklist item is hidden until its group is expanded (collapse is the density fix).
    expect(screen.queryByText("Google OAuth login")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Accounts & auth/ }));
    expect(screen.getByText("Google OAuth login")).toBeInTheDocument();
  });

  it('linkMode "app": each report entry links into the authed /report/:id route', () => {
    render(
      <MemoryRouter>
        <ProjectOverview data={project()} tz="UTC" linkMode="app" />
      </MemoryRouter>,
    );
    // The newest fixture report (id 1003) must be an anchor to its report route.
    const entry = screen.getByText("OAuth underway; search shipped").closest("a");
    expect(entry).not.toBeNull();
    expect(entry).toHaveAttribute("href", "/report/1003");
  });

  it('linkMode "none": reports open via onOpenReport with no router present', () => {
    const onOpenReport = vi.fn();
    // Deliberately NO MemoryRouter: the demo path must render without a router, which is the
    // whole reason the link-less mode exists (a route Link would throw here).
    render(<ProjectOverview data={project()} tz="UTC" linkMode="none" onOpenReport={onOpenReport} />);

    const entry = screen.getByText("OAuth underway; search shipped");
    // No anchors in this mode — the entries are buttons.
    expect(entry.closest("a")).toBeNull();
    fireEvent.click(entry);
    expect(onOpenReport).toHaveBeenCalledWith(1003);
  });

  // --- S2.2: the past project's header treatment -------------------------------------

  it("badges a past project and marks its NEXT DUE as not-applicable", () => {
    // The relay nulls stats.next_due for a past project, so this fixture mirrors the real
    // payload rather than leaving a deadline the server would never have sent.
    const { container } = render(
      <MemoryRouter>
        <ProjectOverview
          data={project({
            lifecycle: "past",
            stats: { ...DEMO_PROJECT.stats, next_due: null },
          })}
          tz="UTC"
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("PAST")).toBeInTheDocument();
    // "—" alone would read as "nothing scheduled" — a live-project statement. The muted
    // class is what makes it read as "this no longer applies".
    expect(container.querySelector(".stat-due.stat-na")).not.toBeNull();
  });

  it("shows no badge and a normal NEXT DUE on an active project", () => {
    const { container } = render(
      <MemoryRouter>
        <ProjectOverview data={project({ lifecycle: "active" })} tz="UTC" />
      </MemoryRouter>,
    );
    expect(screen.queryByText("PAST")).toBeNull();
    expect(container.querySelector(".stat-na")).toBeNull();
  });

  it("drops the caller's discussion slot into the right column", () => {
    render(
      <MemoryRouter>
        <ProjectOverview
          data={project()}
          tz="UTC"
          discussion={<div data-testid="discussion-slot">thread goes here</div>}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("discussion-slot")).toBeInTheDocument();
  });
});
