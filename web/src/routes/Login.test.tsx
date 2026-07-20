// =============================================================================
// web/src/routes/Login.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the login page's two credential modes (name+password by
//                  default, access key as the transition fallback), the GENERIC failure
//                  message, and the public-showcase escape hatch — the link appears ONLY
//                  when the relay exposes a showcase, so a gated relay with no showcase
//                  never shows a dead-end link.
// Approach: render the gated, unauthenticated login under ThemeProvider + MemoryRouter
//           (the theme switcher + the showcase Link need both); mock the login client.
// =============================================================================

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Login } from "./Login";
import { ThemeProvider } from "../theme/ThemeProvider";
import type { Me } from "../api/types";

vi.mock("../api/client", () => ({ login: vi.fn(), loginWithPassword: vi.fn() }));

import { login, loginWithPassword } from "../api/client";

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

describe("Login — credential modes", () => {
  beforeEach(() => {
    vi.mocked(login).mockReset();
    vi.mocked(loginWithPassword).mockReset();
  });

  it("defaults to name + password, the interactive credential", () => {
    renderLogin(gatedMe(false));
    expect(screen.getByLabelText(/^name$/i)).toBeTruthy();
    expect(screen.getByLabelText(/^password$/i)).toBeTruthy();
    expect(screen.queryByLabelText(/access key/i)).toBeNull();
  });

  it("submits name + password to the password endpoint", async () => {
    vi.mocked(loginWithPassword).mockResolvedValue({ ok: true, user: null } as never);
    renderLogin(gatedMe(false));

    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "dad" } });
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "a-passphrase" } });
    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    await waitFor(() => expect(loginWithPassword).toHaveBeenCalledWith("dad", "a-passphrase"));
    expect(login).not.toHaveBeenCalled();
  });

  it("can fall back to key mode for an account with no password yet", async () => {
    vi.mocked(login).mockResolvedValue({ ok: true, user: null } as never);
    renderLogin(gatedMe(false));

    fireEvent.click(screen.getByRole("button", { name: /access key instead/i }));
    fireEvent.change(screen.getByLabelText(/access key/i), { target: { value: "a-raw-key" } });
    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    await waitFor(() => expect(login).toHaveBeenCalledWith("a-raw-key"));
    expect(loginWithPassword).not.toHaveBeenCalled();
  });

  it("shows ONE generic message on failure, revealing nothing about the cause", async () => {
    // Why this matters: the relay answers a single generic 401 for unknown name, wrong
    // password, and lockout alike. A UI that said "no such user" would undo that on the
    // client and hand back the account-enumeration oracle the server refuses to give.
    vi.mocked(loginWithPassword).mockResolvedValue({ ok: false } as never);
    renderLogin(gatedMe(false));

    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "ghost" } });
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "whatever" } });
    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    const error = await screen.findByText(/invalid credentials/i);
    expect(error.textContent).not.toMatch(/no such|unknown|not found|wrong password/i);
  });
});
