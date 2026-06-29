// =============================================================================
// web/src/routes/Login.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the login page's public-showcase escape hatch — the link
//                  appears ONLY when the relay exposes a showcase (showcase_enabled),
//                  so a gated relay with no showcase never shows a dead-end link.
// Approach: render the gated, unauthenticated login under ThemeProvider + MemoryRouter
//           (the theme switcher + the showcase Link need both); mock the login client.
// =============================================================================

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { Login } from "./Login";
import { ThemeProvider } from "../theme/ThemeProvider";
import type { Me } from "../api/types";

vi.mock("../api/client", () => ({ login: vi.fn() }));

function gatedMe(showcaseEnabled: boolean): Me {
  return {
    gated: true,
    authenticated: false,
    identity: null,
    scope: { unrestricted: false, projects: [] },
    display_tz: "UTC",
    showcase_enabled: showcaseEnabled,
  };
}

function renderLogin(me: Me) {
  return render(
    <ThemeProvider>
      <MemoryRouter>
        <Login me={me} onAuthChange={() => {}} />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("Login — public showcase escape hatch", () => {
  it("links to the showcase when it is enabled", () => {
    renderLogin(gatedMe(true));
    expect(screen.getByRole("link", { name: /public showcase/i })).toHaveAttribute(
      "href",
      "/showcase",
    );
  });

  it("hides the link when the showcase is disabled (no dead-end)", () => {
    renderLogin(gatedMe(false));
    expect(screen.queryByRole("link", { name: /public showcase/i })).toBeNull();
  });
});
