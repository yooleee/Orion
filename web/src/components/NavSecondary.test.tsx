// =============================================================================
// web/src/components/NavSecondary.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the secondary-block pieces extracted in DR1-R U2 — the ones
//                  now shared by BOTH the desktop sidebar and the mobile "More" sheet, so a
//                  break would hit both navs at once. AccountCard must render the signed-in
//                  identity and fire logout, and render NOTHING when there is no identity;
//                  ShowcaseLink must link to the public Showcase and run its close callback.
// =============================================================================

import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AccountCard, ShowcaseLink } from "./NavSecondary";
import type { Identity } from "../api/types";

const IDENTITY: Identity = { name: "Ada", role: "admin" };

describe("AccountCard", () => {
  it("renders the identity (avatar initial, name, role) and fires logout", () => {
    const onLogout = vi.fn();
    render(<AccountCard identity={IDENTITY} onLogout={onLogout} />);

    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("admin")).toBeInTheDocument();
    // The avatar shows the uppercased first character of the name.
    expect(screen.getByText("A")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it("renders nothing on an anonymous relay (no identity)", () => {
    // An open/anonymous relay has no account — the card must be absent, not an empty shell.
    const { container } = render(<AccountCard identity={null} onLogout={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("appends an extra class (the mobile sheet's more-account) onto the base account class", () => {
    const { container } = render(
      <AccountCard identity={IDENTITY} onLogout={() => {}} className="more-account" />,
    );
    const card = container.querySelector(".account");
    expect(card).toHaveClass("more-account");
  });
});

describe("ShowcaseLink", () => {
  it("links to the public Showcase and runs its onNavigate callback on click", () => {
    const onNavigate = vi.fn();
    render(
      <MemoryRouter>
        <ShowcaseLink onNavigate={onNavigate} />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", { name: /Public showcase/ });
    expect(link).toHaveAttribute("href", "/showcase");

    // The mobile sheet passes onNavigate to close itself as it navigates.
    fireEvent.click(link);
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });
});
