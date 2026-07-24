// =============================================================================
// web/src/routes/Project.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the C3 Inc 2 per-producer checklist section — one card per
//                  contributor when there are two or more, and NO section for a single
//                  producer (whose card would just duplicate the aggregate) or none — and
//                  the Unit 5 "Working agreements" section (cards + freshness, absent when null).
// Approach: mock the API client; provide the shell's outlet context (me) via a Route +
//           Outlet wrapper (Project reads useOutletContext); assert with findByText/queryByText.
// =============================================================================

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { Project } from "./Project";
import { getProject } from "../api/client";
import type {
  ChecklistItem,
  Me,
  ProducerChecklist,
  ProjectDetail,
  ProjectDisciplines,
} from "../api/types";
import type { ShellContext } from "../components/Shell";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, getProject: vi.fn() };
});
const mockGet = vi.mocked(getProject);

beforeEach(() => mockGet.mockReset());

const ME: Me = {
  gated: false,
  authenticated: true,
  identity: { name: "root", role: "admin" },
  scope: { unrestricted: true, projects: null },
  display_tz: "America/Los_Angeles",
  showcase_enabled: false,
};

function item(text: string, done = false): ChecklistItem {
  return {
    text,
    done,
    due_date: null,
    key: text,
    group: null,
    state: done ? "done" : "not_started",
    status: null,
    slipping: false,
  };
}

function producer(name: string, items: ChecklistItem[]): ProducerChecklist {
  const done = items.filter((i) => i.done).length;
  return {
    author_name: name,
    progress: { done, total: items.length, pct: items.length ? Math.round((done / items.length) * 100) : null },
    items,
  };
}

function detail(
  producers: ProducerChecklist[],
  disciplines: ProjectDisciplines | null = null,
): ProjectDetail {
  return {
    name: "demo",
    kind: "project",
    about: null,
    stats: { progress: { done: 0, total: 0, pct: null }, next_due: null, reports_count: 0 },
    milestones: [],
    checklist: [],
    producer_checklists: producers,
    reports: [],
    discussions: [],
    disciplines,
  };
}

// Project reads useOutletContext<ShellContext>(), so it must render under an Outlet that
// provides `me` — a bare MemoryRouter (as ReportTimeline uses) is not enough here.
function renderProject() {
  return render(
    <MemoryRouter initialEntries={["/project/demo"]}>
      <Routes>
        <Route element={<Outlet context={{ me: ME, portfolio: null } satisfies ShellContext} />}>
          <Route path="/project/:name" element={<Project />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Project — per-producer checklists", () => {
  it("renders one card per contributor when there are two or more", async () => {
    mockGet.mockResolvedValue(
      detail([
        producer("Teammate B", [item("B1", true), item("B2")]),
        producer("Teammate C", [item("C1")]),
      ]),
    );
    const { container } = renderProject();

    expect(await screen.findByText("By contributor")).toBeInTheDocument();
    expect(screen.getByText("Teammate B")).toBeInTheDocument();
    expect(screen.getByText("Teammate C")).toBeInTheDocument();
    expect(container.querySelectorAll(".producer-card")).toHaveLength(2);
    expect(screen.getByText("1/2")).toBeInTheDocument(); // Teammate B's progress count
    expect(screen.getByText("0/1")).toBeInTheDocument(); // Teammate C's progress count
  });

  it("renders no per-producer section for a single producer (would duplicate the aggregate)", async () => {
    mockGet.mockResolvedValue(detail([producer("Teammate B", [item("B1")])]));
    renderProject();

    // Wait for load, then assert the section is absent.
    expect(await screen.findByRole("heading", { name: "demo" })).toBeInTheDocument();
    expect(screen.queryByText("By contributor")).toBeNull();
  });

  it("renders no per-producer section when there are no producer checklists", async () => {
    mockGet.mockResolvedValue(detail([]));
    renderProject();

    expect(await screen.findByRole("heading", { name: "demo" })).toBeInTheDocument();
    expect(screen.queryByText("By contributor")).toBeNull();
  });
});

// Unit 5: the "Working agreements" section renders a project's discipline cards (all of
// them, regardless of scope) plus a freshness stamp, and is absent when the project has
// none — the null case a project that never pushed disciplines returns.
describe("Project — Working agreements", () => {
  it("renders the section with each card, its source footer, and the freshness date", async () => {
    mockGet.mockResolvedValue(
      detail([], {
        updated_at: "2026-06-27T17:00:00+00:00",
        cards: [
          { title: "Secrets stay local", why: "Redacted before anything leaves.", source: "docs/security.md" },
          { title: "Tests before merge", why: "Green suite gates the merge.", source: "docs/testing.md" },
        ],
      }),
    );
    renderProject();

    expect(await screen.findByText("Working agreements")).toBeInTheDocument();
    expect(screen.getByText("Secrets stay local")).toBeInTheDocument();
    expect(screen.getByText("Tests before merge")).toBeInTheDocument();
    // The per-card footer still names each card's own source doc.
    expect(screen.getByText("observed · docs/security.md")).toBeInTheDocument();
    // The section-level freshness stamp shows the push date (ME's tz is Los Angeles, so the
    // 17:00 UTC push is still Jun 27 locally).
    expect(screen.getByText(/updated Jun 27, 2026/)).toBeInTheDocument();
  });

  it("renders no section when the project has no disciplines (null)", async () => {
    mockGet.mockResolvedValue(detail([], null));
    renderProject();

    expect(await screen.findByRole("heading", { name: "demo" })).toBeInTheDocument();
    expect(screen.queryByText("Working agreements")).toBeNull();
  });
});

// KB surface (Unit 2): the About line under the project title — what the project IS,
// observed from its doc. (The old always-null `description` gap field retired in DR1-R U3.)
describe("Project — About band", () => {
  it("renders the About line under the title when present", async () => {
    const d = detail([]);
    d.about = "Orion turns project activity into progress updates.";
    mockGet.mockResolvedValue(d);
    renderProject();

    expect(await screen.findByRole("heading", { name: "demo" })).toBeInTheDocument();
    expect(
      screen.getByText("Orion turns project activity into progress updates."),
    ).toBeInTheDocument();
  });

  it("renders no About line when the project set none (null)", async () => {
    mockGet.mockResolvedValue(detail([])); // about defaults to null
    const { container } = renderProject();

    await screen.findByRole("heading", { name: "demo" });
    // No page-sub renders when the project set no About line.
    expect(container.querySelector(".page-sub")).toBeNull();
  });
});
