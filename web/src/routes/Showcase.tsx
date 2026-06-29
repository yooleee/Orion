// =============================================================================
// web/src/routes/Showcase.tsx
// -----------------------------------------------------------------------------
// Responsible for: The public, no-login Showcase — a curated, full-bleed landing
//                  page sharing a handful of projects (GET /api/showcase). Renders
//                  outside the Shell (like Login), with its own top bar and no sidebar.
// Role in project: The guest surface (design/README §10, desktop-08). Read-only and
//                  anonymous: it shows ONLY the summary cards the relay curates, so no
//                  checklist/report/comment/deadline is ever exposed without a login.
//                  A disabled relay 404s the endpoint → this screen shows "not available".
// Security: card text (name, description) comes from the API and renders as inert React
//           text children — never dangerouslySetInnerHTML. A guarantee-test pins this.
// =============================================================================

import { Link } from "react-router-dom";
import { getShowcase } from "../api/client";
import { ApiError } from "../api/client";
import { useApiData } from "../lib/useApiData";
import type { ShowcaseCard } from "../api/types";
import { DEMO_SHOWCASE_CARD } from "../demo/demoData";
import { ProgressBar } from "../components/ProgressBar";
import { ThemeSwitcher } from "../components/ThemeSwitcher";

// How Orion works, in three lines — static product chrome (the framing of the tool
// itself, not any user's project content), so it is hard-coded rather than observed.
const HOW_I_WORK: Array<{ n: string; title: string; body: string }> = [
  {
    n: "01",
    title: "Observe & reframe",
    body: "Orion reads real activity — commits, checklists, sessions — and reframes it into readable progress. It never authors your plans.",
  },
  {
    n: "02",
    title: "Memory over time",
    body: "Deadlines, milestones, and slippage are remembered across updates, so the story of a project builds up rather than resetting each time.",
  },
  {
    n: "03",
    title: "Legible to anyone",
    body: "Every state carries a glyph, a label, and a colour, so the work stays legible to a reviewer at a glance.",
  },
];

/**
 * One curated project card: name + status pill, description, completion + report count,
 * progress bar. Not a link — the Showcase is a single landing page with no public
 * drill-down (the privacy boundary established server-side).
 */
function ShowcaseProjectCard({ card }: { card: ShowcaseCard }) {
  const pct = card.progress.pct ?? 0;
  // "1 report" vs "12 reports" — small touch, but a card reading "1 reports" looks broken.
  const reports = `${card.report_count} report${card.report_count === 1 ? "" : "s"}`;
  return (
    <div className="showcase-card">
      <div className="showcase-card-head">
        <span className="showcase-card-name">{card.name}</span>
        {/* The pill text IS the semantic status; the modifier class only colours it. */}
        <span className={`showcase-status showcase-status-${card.status}`}>{card.status}</span>
      </div>
      {card.description && <p className="showcase-card-desc">{card.description}</p>}
      <div className="showcase-card-meta">
        <span>{pct}% complete</span>
        <span>{reports}</span>
      </div>
      <ProgressBar progress={card.progress} />
    </div>
  );
}

export function Showcase() {
  const { data, error, loading } = useApiData(() => getShowcase(), []);

  return (
    <div className="showcase-screen" data-testid="showcase-screen">
      {/* Top bar — the only chrome; full-bleed below it. */}
      <header className="showcase-topbar">
        <div className="showcase-brand">
          <span className="brand-dot" aria-hidden="true" />
          <span className="showcase-wordmark">Orion</span>
          <span className="showcase-pill">SHOWCASE</span>
        </div>
        <div className="showcase-topbar-right">
          <ThemeSwitcher compact />
          <Link to="/" className="showcase-dash-link">
            ← Dashboard
          </Link>
        </div>
      </header>

      <main className="showcase-body">
        <section className="showcase-hero">
          <div className="eyebrow">A local-first knowledge base that observes &amp; reframes</div>
          <h1 className="showcase-hero-title">Work in progress, made legible.</h1>
          <p className="showcase-hero-sub">
            Orion turns real project activity (commits, checklists, sessions) into readable
            progress. Below is a curated look at what it's tracking.
          </p>
        </section>

        {/* Try it — a clickable, fabricated sample project (static frontend data, no login,
            no backend). Lets a guest see how Orion presents a monitored project. */}
        <section className="showcase-section">
          <div className="showcase-section-head">
            <h2>Try it</h2>
            <span className="showcase-rule" aria-hidden="true" />
          </div>
          <p className="showcase-section-note">
            An interactive sample — click to explore how Orion presents a monitored project.
          </p>
          <Link to="/showcase/demo" className="showcase-card showcase-card-link" data-testid="demo-card">
            <div className="showcase-card-head">
              <span className="showcase-card-name">{DEMO_SHOWCASE_CARD.name}</span>
              <span className="showcase-status showcase-status-active">sample</span>
            </div>
            <p className="showcase-card-desc">{DEMO_SHOWCASE_CARD.description}</p>
            <div className="showcase-card-meta">
              <span>{DEMO_SHOWCASE_CARD.progress.pct}% complete</span>
              <span className="demo-card-cta">Explore the sample →</span>
            </div>
            <ProgressBar progress={DEMO_SHOWCASE_CARD.progress} />
          </Link>
        </section>

        {/* Selected projects — the only data-driven part. While loading or unavailable we
            still render the static hero + How-I-work, so the page never looks broken. */}
        <section className="showcase-section">
          <div className="showcase-section-head">
            <h2>Selected projects</h2>
            <span className="showcase-rule" aria-hidden="true" />
          </div>
          <p className="showcase-section-note">
            Live, read-only summaries — the same card a real project produces.
          </p>
          {loading && <p className="showcase-note">Loading…</p>}
          {error && (
            <p className="showcase-note">
              {error instanceof ApiError && error.status === 404
                ? "The public showcase is not available."
                : "Could not load the showcase."}
            </p>
          )}
          {data && (
            <div className="showcase-grid">
              {data.projects.map((card) => (
                <ShowcaseProjectCard key={card.name} card={card} />
              ))}
            </div>
          )}
        </section>

        <section className="showcase-section">
          <div className="showcase-section-head">
            <h2>How I work</h2>
            <span className="showcase-rule" aria-hidden="true" />
          </div>
          <div className="showcase-howiwork">
            {HOW_I_WORK.map((item) => (
              <div key={item.n} className="showcase-how-card">
                <span className="showcase-how-num">{item.n}</span>
                <h3 className="showcase-how-title">{item.title}</h3>
                <p className="showcase-how-body">{item.body}</p>
              </div>
            ))}
          </div>
        </section>

        <footer className="showcase-footer">
          A curated, read-only view · no sign-in required.
        </footer>
      </main>
    </div>
  );
}
