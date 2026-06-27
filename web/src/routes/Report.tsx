// =============================================================================
// web/src/routes/Report.tsx
// -----------------------------------------------------------------------------
// Responsible for: One progress report in full — breadcrumb + header (with prev/next
//                  nav), the body card + comments on the left, and the context rail on
//                  the right.
// Role in project: The report reader. Reads /api/reports/:id; a 404 renders a clean
//                  not-found (out-of-scope reports are 404 too — existence-hiding).
// =============================================================================

import { useEffect, useState } from "react";
import { Link, useOutletContext, useParams } from "react-router-dom";
import type { ShellContext } from "../components/Shell";
import type { Comment } from "../api/types";
import { ApiError, getReport } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { relativeTime } from "../lib/time";
import { Breadcrumb } from "../components/Breadcrumb";
import { ReportBody } from "../components/ReportBody";
import { ContextRail } from "../components/ContextRail";
import { CommentList } from "../components/CommentList";
import { CommentComposer } from "../components/CommentComposer";

export function Report() {
  const { id = "" } = useParams();
  const { me } = useOutletContext<ShellContext>();
  const { data, error, loading } = useApiData(() => getReport(id), [id]);
  // Comments posted this session, appended to the fetched thread (no full refetch flicker).
  // Reset when the report changes so they never bleed across navigations.
  const [posted, setPosted] = useState<Comment[]>([]);
  useEffect(() => setPosted([]), [id]);
  const canComment = me.authenticated || !me.gated;

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
          <section className="report-comments">
            <div className="eyebrow block-label">Comments</div>
            <CommentList comments={[...data.comments, ...posted]} />
            <CommentComposer
              reportId={data.id}
              authorName={me.identity?.name ?? null}
              canComment={canComment}
              onPosted={(c) => setPosted((prev) => [...prev, c])}
            />
          </section>
        </div>
        <ContextRail report={data} />
      </div>
    </div>
  );
}
