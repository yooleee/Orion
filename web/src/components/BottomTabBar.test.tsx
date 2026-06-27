// =============================================================================
// web/src/components/BottomTabBar.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the mobile bottom bar's two load-bearing behaviours — every
//                  tab renders a glyph + a TEXT label (legible without colour), and the
//                  active tab matches the current route via the SHARED navActive() rule.
// Why these: the bar is the mobile primary nav; a tab that lit up the wrong section (or
//            lost its text label) would mis-route or fail the colour-independent
//            legibility principle the whole UI holds to.
// =============================================================================

import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { BottomTabBar } from "./BottomTabBar";
import { ThemeProvider } from "../theme/ThemeProvider";
import type { Me } from "../api/types";

const ME: Me = {
  gated: false,
  authenticated: false,
  identity: null,
  scope: { unrestricted: true, projects: null },
  display_tz: "America/Los_Angeles",
  showcase_enabled: false,
};

function renderAt(path: string) {
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <BottomTabBar me={ME} onLogout={() => {}} />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("BottomTabBar", () => {
  it("renders all four tabs as glyph + text label", () => {
    renderAt("/");
    // The text label must be present (legible without relying on the glyph or colour).
    for (const label of ["Projects", "To-dos", "Schedule", "More"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("marks Schedule active on /scheduling, and Projects on a report subpage", () => {
    const { unmount } = renderAt("/scheduling");
    // The active class lands on the tab's link/button; find it via the label's ancestor.
    expect(screen.getByText("Schedule").closest(".tab")).toHaveClass("active");
    expect(screen.getByText("Projects").closest(".tab")).not.toHaveClass("active");
    unmount();

    // Projects owns the report surface too (the shared navActive rule), so /report/26
    // lights Projects, not another tab.
    renderAt("/report/26");
    expect(screen.getByText("Projects").closest(".tab")).toHaveClass("active");
    expect(screen.getByText("Schedule").closest(".tab")).not.toHaveClass("active");
  });

  it("opens the More sheet (theme switcher) when More is tapped", () => {
    renderAt("/");
    // The theme control is only in the DOM once the sheet is open.
    expect(screen.queryByRole("group", { name: "Theme" })).toBeNull();
    fireEvent.click(screen.getByText("More"));
    expect(screen.getByRole("group", { name: "Theme" })).toBeInTheDocument();
  });
});
