// =============================================================================
// web/src/routes/Home.tsx
// -----------------------------------------------------------------------------
// Responsible for: The portfolio home — the sectioned overview (Projects + To-dos),
//                  a scope banner for restricted viewers, and the empty/first-run state.
// Role in project: The dashboard's landing surface and the core IA decision (distinct
//                  sections, not one flat list). Reads /api/portfolio via the shell's
//                  Outlet context (loaded once in Shell) and /api/me for the display zone.
// =============================================================================

import { useOutletContext } from "react-router-dom";
import type { ShellContext } from "../components/Shell";
import { ProjectRow } from "../components/ProjectRow";
import { TrackerCard } from "../components/TrackerCard";
import { ScopeBanner } from "../components/ScopeBanner";
import { EmptyState } from "../components/EmptyState";

export function Home() {
  const { me, portfolio } = useOutletContext<ShellContext>();

  if (portfolio === null) {
    return <div className="center-note">Loading…</div>;
  }

  const { projects, trackers } = portfolio;
  const tz = me.display_tz;
  const isEmpty = projects.length === 0 && trackers.length === 0;

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
                <h2>To-dos</h2>
                <span className="section-rule" />
              </div>
              <div className="row-stack">
                {trackers.map((t) => (
                  <TrackerCard key={t.name} tracker={t} tz={tz} />
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
