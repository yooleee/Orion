// =============================================================================
// web/src/routes/Skills.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the Skills comb's load-bearing properties — skills render
//                  grouped under their categories with the evidence "observed · <projects>"
//                  footer, the empty state shows, and untrusted card text renders INERT.
// Why these: observe-not-originate makes the projects footer the honest anchor, so the
//            grouping + footer must render; and the cards/teeth show observed text from the
//            API, so they carry the SAME XSS guarantee the other route tests pin.
// Approach: mock the API client so no network is hit; render under MemoryRouter (matches
//           the other route tests' harness).
// =============================================================================

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { Skills } from "./Skills";
import { getSkills } from "../api/client";
import type { SkillsData } from "../api/types";

vi.mock("../api/client", () => ({ getSkills: vi.fn() }));
const mockGet = vi.mocked(getSkills);

beforeEach(() => mockGet.mockReset());

function renderSkills() {
  return render(
    <MemoryRouter>
      <Skills />
    </MemoryRouter>,
  );
}

const UNRESTRICTED = { unrestricted: true, projects: null };

describe("Skills — comb grouping + evidence anchor", () => {
  it("renders skills under their category with the observed-projects footer", async () => {
    const data: SkillsData = {
      scope: UNRESTRICTED,
      categories: ["Backend", "ML / NLP"],
      skills: [
        {
          name: "Python stdlib backends",
          category: "Backend",
          depth: 3,
          projects: ["demo-project", "orion"],
          evidence: "Built the relay's stdlib HTTP API.",
          signals: ["git", "docs"],
        },
        {
          name: "LLM pipelines",
          category: "ML / NLP",
          depth: 2,
          projects: ["orion"],
          evidence: "Wrote the Haiku summarizer.",
          signals: ["git"],
        },
      ],
    };
    mockGet.mockResolvedValue(data);
    renderSkills();

    // findBy* waits out the useApiData effect's async resolve.
    expect(await screen.findByRole("heading", { name: "Backend" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "ML / NLP" })).toBeInTheDocument();
    // The skill name appears in both the tooth label and the evidence card.
    expect(screen.getAllByText("Python stdlib backends").length).toBeGreaterThanOrEqual(1);
    // The evidence card anchors the skill to the projects that demonstrate it (the honest
    // observe-not-originate footer), listing BOTH evidencing projects.
    expect(screen.getByText("observed · demo-project, orion")).toBeInTheDocument();
    expect(screen.getByText("Wrote the Haiku summarizer.")).toBeInTheDocument();
  });

  it("shows the empty state when nothing has been observed", async () => {
    mockGet.mockResolvedValue({ scope: UNRESTRICTED, categories: [], skills: [] });
    renderSkills();
    expect(await screen.findByText(/No skills observed yet/)).toBeInTheDocument();
  });
});

describe("Skills — untrusted card text renders inert", () => {
  it("never injects a <script>/<img> from a malicious skill name or evidence", async () => {
    const EVIL_NAME = "<script>window.__pwned_skill=1</script>";
    const EVIL_EVIDENCE = "<img src=x onerror=window.__pwned_skill=1>";
    mockGet.mockResolvedValue({
      scope: UNRESTRICTED,
      categories: ["Backend"],
      skills: [
        {
          name: EVIL_NAME,
          category: "Backend",
          depth: 1,
          projects: ["orion"],
          evidence: EVIL_EVIDENCE,
          signals: [],
        },
      ],
    });
    const { container } = renderSkills();

    await screen.findAllByText(EVIL_NAME);
    // None of the payloads became live DOM.
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as { __pwned_skill?: number }).__pwned_skill).toBeUndefined();
    // The raw markup is present verbatim as inert text.
    expect(container.textContent).toContain(EVIL_NAME);
    expect(container.textContent).toContain(EVIL_EVIDENCE);
  });
});
