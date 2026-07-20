// =============================================================================
// web/src/components/ScopeBanner.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning that the scope banner describes each role's access
//                  TRUTHFULLY — a member's projects are visible, not granted.
// Role in project: The banner is the only place the dashboard explains WHY a view is
//                  narrowed, so wrong wording here misinforms someone about their own
//                  access. Cheap to test, and easy to regress when roles are added.
// =============================================================================

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScopeBanner } from "./ScopeBanner";
import type { Scope } from "../api/types";

const scoped: Scope = { unrestricted: false, projects: ["alpha", "beta"] };

describe("ScopeBanner", () => {
  it("tells a viewer its projects were granted", () => {
    const { container } = render(<ScopeBanner scope={scoped} role="viewer" />);
    expect(container.textContent).toContain("2 projects granted");
  });

  it("does NOT tell a member its projects were granted", () => {
    // A member with zero grants still sees every org-visible project (Unit 5), so
    // "granted" would describe its access wrongly.
    const { container } = render(<ScopeBanner scope={scoped} role="member" />);
    expect(container.textContent).toContain("2 projects visible");
    expect(container.textContent).not.toContain("granted");
    expect(container.textContent).toContain("organization");
  });

  it("renders nothing at all for an unrestricted scope", () => {
    // An admin (or an open relay) has no narrowed view to explain.
    const { container } = render(
      <ScopeBanner scope={{ unrestricted: true, projects: null }} role="admin" />,
    );
    expect(container.textContent).toBe("");
  });

  it("falls back to the granted wording when the role is unknown", () => {
    // The role prop is optional; an omitted role must not render "undefined" or crash.
    const { container } = render(<ScopeBanner scope={scoped} />);
    expect(container.textContent).toContain("granted");
    expect(container.textContent).not.toContain("undefined");
  });
});
