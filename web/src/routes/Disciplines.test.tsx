// =============================================================================
// web/src/routes/Disciplines.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the Disciplines page's load-bearing properties — the
//                  Global vs per-project grouping renders (with the "observed · <source>"
//                  footer), and untrusted card text (title / why / source) renders INERT.
// Why these: observe-not-originate makes the source footer the honest claim, so the
//            grouping + footer must render; and the cards show observed text from the API,
//            so they carry the SAME XSS guarantee the Sidebar/Showcase tests pin.
// Approach: mock the API client so no network is hit; render under MemoryRouter (the page
//           itself needs no router, but this matches the other route tests' harness).
// =============================================================================

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { Disciplines } from "./Disciplines";
import { getDisciplines } from "../api/client";
import type { DisciplinesData } from "../api/types";

vi.mock("../api/client", () => ({ getDisciplines: vi.fn() }));
const mockGet = vi.mocked(getDisciplines);

beforeEach(() => mockGet.mockReset());

function renderDisciplines() {
  return render(
    <MemoryRouter>
      <Disciplines />
    </MemoryRouter>,
  );
}

const UNRESTRICTED = { unrestricted: true, projects: null };

describe("Disciplines — Global vs per-project grouping", () => {
  it("renders a Global group and a per-project group, each card with its source footer", async () => {
    const data: DisciplinesData = {
      scope: UNRESTRICTED,
      global: [
        { title: "Local-first by default", why: "Runs on your machine.", source: "docs/strategy.md" },
      ],
      projects: [
        {
          name: "orion",
          principles: [
            { title: "Observe, never originate", why: "Reads and reframes.", source: "CLAUDE.md" },
          ],
        },
      ],
    };
    mockGet.mockResolvedValue(data);
    renderDisciplines();

    // findBy* waits out the useApiData effect's async resolve.
    expect(await screen.findByText("Global")).toBeInTheDocument();
    expect(screen.getByText("orion")).toBeInTheDocument();
    // Both cards' titles + their observed-source footers are present.
    expect(screen.getByText("Local-first by default")).toBeInTheDocument();
    expect(screen.getByText("observed · docs/strategy.md")).toBeInTheDocument();
    expect(screen.getByText("Observe, never originate")).toBeInTheDocument();
    expect(screen.getByText("observed · CLAUDE.md")).toBeInTheDocument();
  });

  it("shows the empty state when nothing has been observed", async () => {
    mockGet.mockResolvedValue({ scope: UNRESTRICTED, global: [], projects: [] });
    renderDisciplines();
    expect(await screen.findByText(/No disciplines observed yet/)).toBeInTheDocument();
  });
});

describe("Disciplines — untrusted card text renders inert", () => {
  it("never injects a <script>/<img> from a malicious title, why, or source", async () => {
    const EVIL_TITLE = "<script>window.__pwned_disc=1</script>";
    const EVIL_WHY = "<img src=x onerror=window.__pwned_disc=1>";
    const EVIL_SOURCE = "<script>window.__pwned_disc=1</script>.md";
    mockGet.mockResolvedValue({
      scope: UNRESTRICTED,
      global: [{ title: EVIL_TITLE, why: EVIL_WHY, source: EVIL_SOURCE }],
      projects: [],
    });
    const { container } = renderDisciplines();

    await screen.findByText(EVIL_TITLE);
    // None of the payloads became live DOM.
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as { __pwned_disc?: number }).__pwned_disc).toBeUndefined();
    // The raw markup is present verbatim as inert text.
    expect(container.textContent).toContain(EVIL_TITLE);
    expect(container.textContent).toContain(EVIL_WHY);
  });
});
