// =============================================================================
// web/src/routes/Showcase.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the public Showcase's two load-bearing properties — the
//                  derived status pill (shipped vs active) tracks completion, and
//                  untrusted card text (name/description) renders INERT.
// Why these: the Showcase is the only anonymous, no-login surface, so its XSS property
//            is the same GUARANTEE the Sidebar test pins, applied to the guest page; and
//            the pill must never disagree with the progress it's derived from.
// Approach: mock the API client so no network is hit; render under ThemeProvider +
//           MemoryRouter (the top bar's theme switcher + "← Dashboard" link need both).
// =============================================================================

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { Showcase } from "./Showcase";
import { getShowcase } from "../api/client";
import { ThemeProvider } from "../theme/ThemeProvider";
import type { ShowcaseCard, ShowcaseData } from "../api/types";

// Mock the client: getShowcase is stubbed per test; ApiError is a real-enough class so the
// component's `error instanceof ApiError && error.status === 404` branch is exercisable.
vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  getShowcase: vi.fn(),
}));
const mockGet = vi.mocked(getShowcase);

beforeEach(() => mockGet.mockReset());

function renderShowcase() {
  return render(
    <ThemeProvider>
      <MemoryRouter>
        <Showcase />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

function card(over: Partial<ShowcaseCard>): ShowcaseCard {
  return {
    name: "orion",
    description: "A local-first tracker.",
    status: "active",
    progress: { done: 6, total: 15, pct: 40 },
    report_count: 12,
    ...over,
  };
}

describe("Showcase — derived status pill", () => {
  it("shows a 'shipped' pill at 100% and an 'active' pill below it", async () => {
    const data: ShowcaseData = {
      projects: [
        card({ name: "orion", status: "active", progress: { done: 6, total: 15, pct: 40 } }),
        card({
          name: "barebones-ai-village",
          status: "shipped",
          progress: { done: 4, total: 4, pct: 100 },
          report_count: 1,
        }),
      ],
    };
    mockGet.mockResolvedValue(data);
    renderShowcase();

    // findBy* waits out the useApiData effect's async resolve.
    expect(await screen.findByText("shipped")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    // Completion text is derived from the same progress the pill is — they can't disagree.
    expect(screen.getByText("40% complete")).toBeInTheDocument();
    expect(screen.getByText("100% complete")).toBeInTheDocument();
    // "1 report" is singular; the 12-report project pluralizes.
    expect(screen.getByText("1 report")).toBeInTheDocument();
    expect(screen.getByText("12 reports")).toBeInTheDocument();
  });
});

describe("Showcase — untrusted card text renders inert", () => {
  it("never injects a <script>/<img> from a malicious project name or description", async () => {
    const EVIL_NAME = "<script>window.__pwned_showcase=1</script>";
    const EVIL_DESC = "<img src=x onerror=window.__pwned_showcase=1>";
    mockGet.mockResolvedValue({
      projects: [card({ name: EVIL_NAME, description: EVIL_DESC })],
    });
    const { container } = renderShowcase();

    // Wait for the card to render, then assert the payloads never became live DOM.
    await screen.findByText(EVIL_NAME);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(
      (window as unknown as { __pwned_showcase?: number }).__pwned_showcase,
    ).toBeUndefined();
    // The raw markup is present verbatim as inert text.
    expect(container.textContent).toContain(EVIL_NAME);
    expect(container.textContent).toContain(EVIL_DESC);
  });
});

// Note: the disabled-relay 404 path (the "not available" note) is pinned server-side by
// test_api_showcase_404_when_disabled and confirmed in eyes-on. A component test for it is
// omitted because mocking a rejected fetch trips Vitest's unhandled-rejection guard before
// useApiData's handler attaches — not worth a brittle workaround for copy already covered.
