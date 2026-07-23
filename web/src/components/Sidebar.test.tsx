// =============================================================================
// web/src/components/Sidebar.test.tsx
// -----------------------------------------------------------------------------
// GUARANTEE TEST #2 — "untrusted text renders inert".
// Responsible for: Pinning that stored, attacker-influenceable text (here, project and
//                  tracker NAMES from the API) renders as inert TEXT, never as live DOM.
//                  This is the XSS property the old server-rendered dashboard guaranteed
//                  via html.escape; in the SPA it must hold by construction (React's
//                  default text binding — never dangerouslySetInnerHTML for stored data).
// Approach: render the Sidebar with malicious names and assert NO injected element
//           (<script>/<img>) exists and the raw markup appears as literal text.
// =============================================================================

import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { Sidebar } from "./Sidebar";
import { ThemeProvider } from "../theme/ThemeProvider";
import type { Me, Portfolio, ProjectSummary, TrackerSummary } from "../api/types";

const ME: Me = {
  gated: false,
  authenticated: false,
  identity: null,
  scope: { unrestricted: true, projects: null },
  display_tz: "America/Los_Angeles",
  showcase_enabled: false,
};

// A project whose NAME is a script-injection attempt (as if a repo were named this).
const EVIL_PROJECT = "<script>window.__pwned=1</script>";
const EVIL_TRACKER = "<img src=x onerror=window.__pwned=1>";

function portfolioWithEvilNames(): Portfolio {
  const project: ProjectSummary = {
    name: EVIL_PROJECT,
    kind: "project",
    headline: "",
    progress: { done: 0, total: 0, pct: null },
    at_risk: 0,
    slipping: 0,
    next_due: null,
    updated_at: "2026-06-26T00:00:00+00:00",
    report_id: null,
    about: null,
  };
  const tracker: TrackerSummary = {
    name: EVIL_TRACKER,
    kind: "tracker",
    item_count: 0,
    progress: { done: 0, total: 0, pct: null },
    segments: { overdue: 0, due_soon: 0, remaining: 0, done: 0 },
    at_risk: 0,
    slipping: 0,
    next_due: null,
    at_risk_items: [],
    updated_at: "2026-06-26T00:00:00+00:00",
  };
  return { scope: ME.scope, projects: [project], trackers: [tracker] };
}

describe("Sidebar — untrusted names render inert", () => {
  it("never injects a <script> or <img> from a malicious project/tracker name", () => {
    const { container } = render(
      <ThemeProvider>
        <MemoryRouter>
          <Sidebar me={ME} portfolio={portfolioWithEvilNames()} onLogout={() => {}} />
        </MemoryRouter>
      </ThemeProvider>,
    );

    // The injection attempts must NOT have become real elements...
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    // ...the global the payloads tried to set must be untouched...
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    // ...and the raw markup must be present verbatim as inert text.
    expect(container.textContent).toContain(EVIL_PROJECT);
    expect(container.textContent).toContain(EVIL_TRACKER);
  });
});
