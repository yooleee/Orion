// =============================================================================
// web/src/routes/Report.tsx
// -----------------------------------------------------------------------------
// Responsible for: One progress report in full — breadcrumb + header (with prev/next
//                  nav), the body card on the left, and the context rail on the right.
// Role in project: The report reader. Reads /api/reports/:id; a 404 renders a clean
//                  not-found (out-of-scope reports are 404 too — existence-hiding).
//                  The one conversation surface is the project-level Discussion thread
//                  (on the project page); the report page carries no thread of its own
//                  (KI-28 Stage 2 retired per-report comments).
// =============================================================================

import { Link, useParams } from "react-router-dom";
import { ApiError, getReport } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { relativeTime } from "../lib/time";
import { Breadcrumb } from "../components/Breadcrumb";
import { ReportBody } from "../components/ReportBody";
import { ContextRail } from "../components/ContextRail";

export function Report() {
  const { id = "" } = useParams();
  const { data, error, loading } = useApiData(() => getReport(id), [id]);

  if (loading) return <div className="center-note">Loading…</div>;
  if (error) {
    const msg = error instanceof ApiError && error.status === 404 ? `No report #${id}.` : "Could not load this report.";
    return <div className="center-note">{msg}</div>;
  }
  if (!data) return null;

  return (
    <div>
      <Breadcrumb
        items={[
          { label: "projects", to: "/" },
          { label: data.project, to: `/project/${encodeURIComponent(data.project)}` },
          { label: `report #${data.number}` },
        ]}
      />

      <header className="detail-header">
        <div className="detail-headline">
          <h1 className="page-title">{data.project}</h1>
          <p className="page-sub">
            Progress report #{data.number} · {relativeTime(data.generated_at)}
          </p>
        </div>
        <div className="report-nav">
          <Link className="nav-btn" to={`/project/${encodeURIComponent(data.project)}`}>
            ← Back to {data.project}
          </Link>
          {/* Links route by the stable global id; labels show the per-project ordinal. */}
          {data.nav.next_id !== null && (
            <Link className="nav-btn" to={`/report/${data.nav.next_id}`}>
              ← Report #{data.nav.next_number}
            </Link>
          )}
          {data.nav.prev_id !== null && (
            <Link className="nav-btn" to={`/report/${data.nav.prev_id}`}>
              Report #{data.nav.prev_number} →
            </Link>
          )}
        </div>
      </header>

      <div className="report-grid">
        <div className="report-main">
          <ReportBody report={data} />
        </div>
        <ContextRail report={data} />
      </div>
    </div>
  );
}
