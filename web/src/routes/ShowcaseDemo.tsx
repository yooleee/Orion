// =============================================================================
// web/src/routes/ShowcaseDemo.tsx
// -----------------------------------------------------------------------------
// Responsible for: The public, no-login "sample project" walkthrough reached from the
//                  Showcase landing. A guest clicks the demo card and lands here, where
//                  they can browse a fabricated project the way a real one looks in Orion:
//                  the Overview (the shared ProjectOverview, read-only) and each report in
//                  full.
// Role in project: The drill-down half of the public Showcase. Renders ENTIRELY from
//                  web/src/demo/demoData.ts fixtures using the real dashboard components —
//                  since DR1-R U1 that includes the SAME ProjectOverview the real project
//                  page uses (in linkMode="none"), so the demo can no longer drift from it.
//                  It makes NO API calls and all navigation is internal state, so it never
//                  enters the authenticated routes and cannot read any real data.
// Security: there is no backend path here at all — the demo is static frontend data, so a
//           real-data leak is impossible by construction (see the kickoff doc).
// =============================================================================

import { useState } from "react";
import { Link } from "react-router-dom";
import { DEMO_PROJECT, demoReportById } from "../demo/demoData";
import { relativeTime } from "../lib/time";
import { ProjectOverview } from "../components/ProjectOverview";
import { DiscussionList } from "../components/DiscussionList";
import { ReportBody } from "../components/ReportBody";
import { ContextRail } from "../components/ContextRail";
import { ThemeSwitcher } from "../components/ThemeSwitcher";

// A fixed display timezone for the demo's relative-date labels (no logged-in user here).
const DEMO_TZ = "UTC";

/** One report in full, reusing the real ReportBody + ContextRail, with demo-local
 *  back / older / newer navigation (the real Report nav links into authed routes). */
function DemoReport({
  reportId,
  onBack,
  onOpenReport,
}: {
  reportId: number;
  onBack: () => void;
  onOpenReport: (id: number) => void;
}) {
  const report = demoReportById(reportId);
  if (!report) return null;

  return (
    <div>
      <header className="detail-header">
        <div className="detail-headline">
          <h1 className="page-title">{report.project}</h1>
          <p className="page-sub">
            Progress report #{report.number} · {relativeTime(report.generated_at)}
          </p>
        </div>
        <div className="report-nav">
          <button type="button" className="nav-btn" onClick={onBack}>
            ← Back to {report.project}
          </button>
          {report.nav.next_id !== null && (
            <button type="button" className="nav-btn" onClick={() => onOpenReport(report.nav.next_id!)}>
              ← Report #{report.nav.next_number}
            </button>
          )}
          {report.nav.prev_id !== null && (
            <button type="button" className="nav-btn" onClick={() => onOpenReport(report.nav.prev_id!)}>
              Report #{report.nav.prev_number} →
            </button>
          )}
        </div>
      </header>

      <div className="report-grid">
        <div className="report-main">
          <ReportBody report={report} />
          {/* No comments composer: the public demo is read-only. */}
        </div>
        <ContextRail report={report} />
      </div>
    </div>
  );
}

/**
 * The public Showcase demo walkthrough.
 *
 * Returns: a full-bleed page (its own top bar, like the Showcase landing) over a fabricated
 *   project, rendered from fixtures — the overview, or a single report in full.
 *
 * Why: lets a guest experience the real dashboard UI on a sample project without any login
 *   and without any backend access to real data. Since the standalone Disciplines tab retired
 *   (Unit 5 — its cards now live in the overview's "Working agreements" section), the demo is
 *   the single overview surface, so a tab bar would be a one-item bar; a report drill-down is
 *   plain open/close state.
 */
export function ShowcaseDemo() {
  // When set, show that report in full instead of the project overview.
  const [reportId, setReportId] = useState<number | null>(null);

  return (
    <div className="showcase-screen" data-testid="showcase-demo">
      <header className="showcase-topbar">
        <div className="showcase-brand">
          <span className="brand-dot" aria-hidden="true" />
          <span className="showcase-wordmark">Orion</span>
          <span className="showcase-pill">SAMPLE PROJECT</span>
        </div>
        <div className="showcase-topbar-right">
          <ThemeSwitcher compact />
          <Link to="/showcase" className="showcase-dash-link">
            ← Showcase
          </Link>
        </div>
      </header>

      <main className="showcase-body">
        <p className="demo-banner">
          A guided sample project — every value here is fabricated to show how Orion presents
          a monitored project. Read-only.
        </p>

        {reportId !== null ? (
          <DemoReport
            reportId={reportId}
            onBack={() => setReportId(null)}
            onOpenReport={(id) => setReportId(id)}
          />
        ) : (
          // The SAME overview the real project page renders (DR1-R U1), in linkMode="none"
          // so its report timeline opens by internal state instead of linking into an authed
          // route. The discussion slot is a read-only thread — a guest cannot post.
          <ProjectOverview
            data={DEMO_PROJECT}
            tz={DEMO_TZ}
            linkMode="none"
            onOpenReport={(id) => setReportId(id)}
            discussion={
              <section>
                <div className="eyebrow block-label">Discussion</div>
                <DiscussionList items={DEMO_PROJECT.discussions} />
                {/* No composer: the public demo is read-only. */}
              </section>
            }
          />
        )}
      </main>
    </div>
  );
}
