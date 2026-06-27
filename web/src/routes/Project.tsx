// =============================================================================
// web/src/routes/Project.tsx
// -----------------------------------------------------------------------------
// Responsible for: One project's full page — header stats, FORWARD LOOK milestones,
//                  LIVE CHECKLIST, the REPORTS timeline, and COMMENTS (read + inert
//                  composer). Two-column layout per the design.
// Role in project: Everything observed about a project. Reads /api/projects/:name; a
//                  404 renders a clean not-found (existence-hiding carries through).
// =============================================================================

import { useEffect, useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";
import type { ShellContext } from "../components/Shell";
import type { Comment } from "../api/types";
import { ApiError, getProject } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { deadlineLabel } from "../lib/time";
import { STATUS_STYLES } from "../theme/status";
import { Breadcrumb } from "../components/Breadcrumb";
import { MilestoneCard } from "../components/MilestoneCard";
import { ChecklistRow } from "../components/ChecklistRow";
import { ReportTimeline } from "../components/ReportTimeline";
import { CommentList } from "../components/CommentList";
import { CommentComposer } from "../components/CommentComposer";

export function Project() {
  const { name = "" } = useParams();
  const { me } = useOutletContext<ShellContext>();
  const { data, error, loading } = useApiData(() => getProject(name), [name]);
  const tz = me.display_tz;
  // Comments posted this session, appended to the fetched thread; reset on project change.
  const [posted, setPosted] = useState<Comment[]>([]);
  useEffect(() => setPosted([]), [name]);
  const canComment = me.authenticated || !me.gated;

  if (loading) return <div className="center-note">Loading…</div>;
  if (error) {
    const msg = error instanceof ApiError && error.status === 404 ? `No project “${name}”.` : "Could not load this project.";
    return <div className="center-note">{msg}</div>;
  }
  if (!data) return null;

  const nextDue = data.stats.next_due;

  return (
    <div>
      <Breadcrumb items={[{ label: "projects", to: "/" }, { label: data.name }]} />

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
              // Colour by the deadline's state (overdue red vs due-soon amber), not a fixed
              // hue — so an overdue NEXT DUE reads as overdue.
              style={nextDue ? { color: `var(${STATUS_STYLES[nextDue.state].colorVar})` } : undefined}
            >
              {nextDue ? deadlineLabel(nextDue.due_date, tz) : "—"}
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
          {data.milestones.length > 0 && (
            <section>
              <div className="eyebrow block-label">Forward look</div>
              <div className="milestone-stack">
                {data.milestones.map((m) => (
                  <MilestoneCard key={m.group} milestone={m} tz={tz} />
                ))}
              </div>
            </section>
          )}

          {data.checklist.length > 0 && (
            <section>
              <div className="eyebrow block-label">Live checklist</div>
              <div className="check-list">
                {data.checklist.map((item, i) => (
                  <ChecklistRow key={item.key ?? `${item.text}-${i}`} item={item} tz={tz} />
                ))}
              </div>
            </section>
          )}
        </div>

        <div className="detail-right">
          <section>
            <div className="eyebrow block-label">Reports</div>
            <ReportTimeline reports={data.reports} />
          </section>

          <section>
            <div className="eyebrow block-label">Comments</div>
            <CommentList comments={[...data.comments, ...posted]} />
            {/* A project's comments attach to its latest report (newest-first → [0]). */}
            <CommentComposer
              reportId={data.reports[0]?.id ?? null}
              authorName={me.identity?.name ?? null}
              canComment={canComment}
              onPosted={(c) => setPosted((prev) => [...prev, c])}
            />
          </section>
        </div>
      </div>
    </div>
  );
}
