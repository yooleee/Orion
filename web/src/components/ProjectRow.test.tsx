// =============================================================================
// tests/ProjectRow.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the Home project row's About sub-line (KB surface Unit 2)
//                  — it renders the observed About when present, omits it when null,
//                  and stays DISTINCT from the latest-report headline.
// Why these: About is the row's "what this project is" identity line; conflating it
//            with the headline (the latest-report pulse) or rendering an empty line
//            when unset would muddy the Home reading order the band exists to add.
// =============================================================================

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ProjectRow } from "./ProjectRow";
import type { ProjectSummary } from "../api/types";

function row(overrides: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    name: "orion",
    kind: "project",
    lifecycle: "active",
    headline: "Shipped the auth revamp",
    progress: { done: 6, total: 15, pct: 40 },
    at_risk: 0,
    slipping: 0,
    next_due: null,
    updated_at: "2026-07-22T10:00:00+00:00",
    report_id: 26,
    about: null,
    ...overrides,
  };
}

function renderRow(project: ProjectSummary) {
  return render(
    <MemoryRouter>
      <ProjectRow project={project} tz="America/Los_Angeles" />
    </MemoryRouter>,
  );
}

describe("ProjectRow — About sub-line", () => {
  it("renders the About line and the headline as distinct lines when About is set", () => {
    const { container } = renderRow(
      row({ about: "Turns project activity into progress updates." }),
    );
    // About shows as its own .row-about line, separate from the .row-headline pulse.
    const about = container.querySelector(".row-about");
    expect(about?.textContent).toBe("Turns project activity into progress updates.");
    expect(container.querySelector(".row-headline")?.textContent).toBe(
      "Shipped the auth revamp",
    );
    // Both are present — About did not replace the headline.
    expect(screen.getByText("Turns project activity into progress updates.")).toBeInTheDocument();
    expect(screen.getByText("Shipped the auth revamp")).toBeInTheDocument();
  });

  it("renders no About line when the project set none (null)", () => {
    const { container } = renderRow(row({ about: null }));
    // Absent, not an empty line: the .row-about element must not render at all.
    expect(container.querySelector(".row-about")).toBeNull();
    // The headline still renders (the two are independent).
    expect(container.querySelector(".row-headline")?.textContent).toBe(
      "Shipped the auth revamp",
    );
  });
});
