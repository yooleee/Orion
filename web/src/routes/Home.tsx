// =============================================================================
// web/src/routes/Home.tsx
// -----------------------------------------------------------------------------
// Responsible for: The portfolio home — the sectioned overview (Projects + Trackers, with
//                  finished work grouped into a collapsed Past projects section), a scope
//                  banner for restricted viewers, and the empty/first-run state.
// Role in project: The dashboard's landing surface and the core IA decision (distinct
//                  sections, not one flat list). Reads /api/portfolio via the shell's
//                  Outlet context (loaded once in Shell) and /api/me for the display zone.
// =============================================================================

import { useOutletContext } from "react-router-dom";
import type { ShellContext } from "../components/Shell";
import { ProjectRow } from "../components/ProjectRow";
import { TrackerCard } from "../components/TrackerCard";
import { PastProjects } from "../components/PastProjects";
import { ScopeBanner } from "../components/ScopeBanner";
import { EmptyState } from "../components/EmptyState";

export function Home() {
  const { me, portfolio } = useOutletContext<ShellContext>();

  if (portfolio === null) {
    return <div className="center-note">Loading…</div>;
  }

  const tz = me.display_tz;
  // S2.2: finished work leaves the live sections entirely and groups below (one section for
  // both kinds), so the top of the home is only what is still running. The split is done
  // here rather than server-side because the relay ships one portfolio and the grouping is
  // a presentation choice — the same reasoning that keeps the ≤2-signal rule client-side.
  const projects = portfolio.projects.filter((p) => p.lifecycle !== "past");
  const trackers = portfolio.trackers.filter((t) => t.lifecycle !== "past");
  const pastProjects = portfolio.projects.filter((p) => p.lifecycle === "past");
  const pastTrackers = portfolio.trackers.filter((t) => t.lifecycle === "past");
  // The first-run state means "nothing at all", so it must count past work too — otherwise
  // a portfolio of only finished projects would show the "no projects yet" onboarding copy
  // above a section that plainly contains projects.
  const isEmpty =
    portfolio.projects.length === 0 && portfolio.trackers.length === 0;

  return (
    <div>
      <div className="page-header">
        <div className="eyebrow">Portfolio overview</div>
        <h1 className="page-title">Everything, observed.</h1>
        <p className="page-sub">
          Reframed from your git history, checklists &amp; Claude Code sessions — read &amp;
          comment only.
        </p>
      </div>

      <ScopeBanner scope={portfolio.scope} role={me.identity?.role ?? null} />

      {isEmpty ? (
        <EmptyState />
      ) : (
        <>
          {projects.length > 0 && (
            <section className="home-section">
              <div className="section-header">
                <h2>Projects</h2>
                <span className="section-rule" />
              </div>
              <div className="row-stack">
                {projects.map((p) => (
                  <ProjectRow key={p.name} project={p} tz={tz} />
                ))}
              </div>
            </section>
          )}

          {trackers.length > 0 && (
            <section className="home-section">
              <div className="section-header">
                <h2>Trackers</h2>
                <span className="section-rule" />
              </div>
              <div className="row-stack">
                {trackers.map((t) => (
                  <TrackerCard key={t.name} tracker={t} tz={tz} />
                ))}
              </div>
            </section>
          )}

          {/* Finished work, collapsed. Renders nothing at all when there is none. */}
          <PastProjects projects={pastProjects} trackers={pastTrackers} tz={tz} />
        </>
      )}
    </div>
  );
}
