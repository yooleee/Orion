// =============================================================================
// web/src/components/DisciplineCard.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the shared DisciplineCard's two load-bearing properties —
//                  (1) it renders the title, why, and "observed · <source>" footer, and
//                  (2) untrusted card text renders as INERT React children (never HTML),
//                  the security guarantee the retired Disciplines route test used to hold.
// Approach: render the component directly (no router/API needed) and assert on the DOM,
//           including that an HTML-looking title is shown as literal text, not parsed.
// =============================================================================

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DisciplineCard } from "./DisciplineCard";

describe("DisciplineCard", () => {
  it("renders the title, why, and observed-source footer", () => {
    render(
      <DisciplineCard
        card={{
          title: "Secrets stay local",
          why: "Redacted before anything leaves the machine.",
          source: "docs/security.md",
        }}
      />,
    );

    expect(screen.getByText("Secrets stay local")).toBeInTheDocument();
    expect(screen.getByText("Redacted before anything leaves the machine.")).toBeInTheDocument();
    expect(screen.getByText("observed · docs/security.md")).toBeInTheDocument();
  });

  it("renders untrusted text inert — an HTML-looking title is shown literally, not parsed", () => {
    // The card text is observed from the API (untrusted); a title that looks like markup must
    // appear as literal characters, never become a real DOM element (no dangerouslySetInnerHTML).
    const { container } = render(
      <DisciplineCard
        card={{ title: "<img src=x onerror=alert(1)>", why: "why", source: "docs/x.md" }}
      />,
    );

    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    // No actual <img> was created from the injected string.
    expect(container.querySelector("img")).toBeNull();
  });
});
