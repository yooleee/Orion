// =============================================================================
// web/src/routes/ShowcaseDemo.tsx
// -----------------------------------------------------------------------------
// Responsible for: The public, no-login "sample project" walkthrough reached from the
//                  Showcase landing. A guest clicks the demo card and lands here, where
//                  they can browse a fabricated project the way a real one looks in Orion:
//                  the Overview (stats, working agreements, milestones, checklist, reports,
//                  discussion) and each report in full.
// Role in project: The drill-down half of the public Showcase. Renders ENTIRELY from
//                  web/src/demo/demoData.ts fixtures using the real dashboard components.
//                  It makes NO API calls and all navigation is internal state, so it never
//                  enters the authenticated routes and cannot read any real data.
// Security: there is no backend path here at all — the demo is static frontend data, so a
//           real-data leak is impossible by construction (see the kickoff doc).
// =============================================================================

import { useState } from "react";
import { Link } from "react-router-dom";
import { DEMO_PROJECT, demoReportById } from "../demo/demoData";
import { deadlineLabel, formatDate, relativeTime } from "../lib/time";
import { STATUS_STYLES } from "../theme/status";
import { MilestoneCard } from "../components/MilestoneCard";
import { ChecklistRow } from "../components/ChecklistRow";
import { DisciplineCard } from "../components/DisciplineCard";
import { DiscussionList } from "../components/DiscussionList";
import { ReportBody } from "../components/ReportBody";
import { ContextRail } from "../components/ContextRail";
import { ThemeSwitcher } from "../components/ThemeSwitcher";

// A fixed display timezone for the demo's relative-date labels (no logged-in user here).
const DEMO_TZ = "UTC";

/** The Overview body: header stats + forward-look milestones + checklist + reports +
 *  the (read-only) discussion thread — the same composition as a real project page,
 *  minus the live composer (a guest cannot post). `onOpenReport` switches to a report. */
function DemoOverview({ onOpenReport }: { onOpenReport: (id: number) => void }) {
  const data = DEMO_PROJECT;
  const nextDue = data.stats.next_due;

  return (
    <div>
      <header className="detail-header">
        <div className="detail-headline">
          <h1 className="page-title">{data.name}</h1>
          {data.description && <p className="page-sub">{data.description}</p>}
        </div>
        <div className="stat-blocks">
          <div className="stat">
            <div className="stat-label">Progress</div>
            <div className="stat-value">
              {data.stats.progress.done} <span className="stat-sub">/ {data.stats.progress.total}</span>
            </div>
          </div>
          <div className="stat">
            <div className="stat-label">Next due</div>
            <div
              className="stat-value stat-due"
              style={nextDue ? { color: `var(${STATUS_STYLES[nextDue.state].colorVar})` } : undefined}
            >
              {nextDue ? deadlineLabel(nextDue.due_date, DEMO_TZ) : "—"}
            </div>
          </div>
          <div className="stat">
            <div className="stat-label">Reports</div>
            <div className="stat-value">{data.stats.reports_count}</div>
          </div>
        </div>
      </header>

      <div className="detail-grid">
        <div className="detail-left">
          {/* Unit 5: the same "Working agreements" section the real project page shows,
              leading the left column (the standalone Disciplines tab retired). */}
          {data.disciplines && data.disciplines.cards.length > 0 && (
            <section>
              <div className="eyebrow block-label">Working agreements</div>
              <div className="disc-freshness">
                Observed in your docs · updated {formatDate(data.disciplines.updated_at, DEMO_TZ)}
              </div>
              <div className="disc-grid">
                {data.disciplines.cards.map((card, i) => (
                  <DisciplineCard key={`${card.title}-${i}`} card={card} />
                ))}
              </div>
            </section>
          )}

          {data.milestones.length > 0 && (
            <section>
              <div className="eyebrow block-label">Forward look</div>
              <div className="milestone-stack">
                {data.milestones.map((m) => (
                  <MilestoneCard key={m.group} milestone={m} tz={DEMO_TZ} />
                ))}
              </div>
            </section>
          )}

          {data.checklist.length > 0 && (
            <section>
              <div className="eyebrow block-label">Live checklist</div>
              <div className="check-list">
                {data.checklist.map((item, i) => (
                  <ChecklistRow key={item.key ?? `${item.text}-${i}`} item={item} tz={DEMO_TZ} />
                ))}
              </div>
            </section>
          )}
        </div>

        <div className="detail-right">
          <section>
            <div className="eyebrow block-label">Reports</div>
            {/* A demo-local reports list (the real ReportTimeline links into authed routes,
                so the demo navigates by internal state instead). */}
            <div className="timeline">
              {data.reports.map((r, i) => (
                <button
                  type="button"
                  key={r.id}
                  className="timeline-entry demo-report-entry"
                  onClick={() => onOpenReport(r.id)}
                >
                  {/* The most-recent entry (index 0) gets the accent dot; older are faint. */}
                  <span className={`timeline-dot${i === 0 ? " latest" : ""}`} aria-hidden="true" />
                  <div className="timeline-body">
                    <div className="timeline-title">{r.title || `Report #${r.number}`}</div>
                    <div className="timeline-meta">
                      #{r.number} · {relativeTime(r.generated_at)}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </section>

          <section>
            <div className="eyebrow block-label">Discussion</div>
            <DiscussionList items={data.discussions} />
            {/* No composer: the public demo is read-only. */}
          </section>
        </div>
      </div>
    </div>
  );
}

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
          <DemoOverview onOpenReport={(id) => setReportId(id)} />
        )}
      </main>
    </div>
  );
}
