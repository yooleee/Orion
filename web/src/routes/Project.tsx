// =============================================================================
// web/src/routes/Project.tsx
// -----------------------------------------------------------------------------
// Responsible for: One project's full page — header stats, FORWARD LOOK milestones,
//                  LIVE CHECKLIST, the REPORTS timeline, and the project-level DISCUSSION
//                  thread (the one conversation surface here). Two-column layout.
// Note: per-report COMMENTS live on the report-detail page, not here — the project-level
//       conversation is the Discussion thread (E2 Inc 5). Keeping both on this page would
//       be two near-identical surfaces doing one job; they were consolidated to Discussion.
// Role in project: Everything observed about a project. Reads /api/projects/:name; a
//                  404 renders a clean not-found (existence-hiding carries through).
// =============================================================================

import { useEffect, useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";
import type { ShellContext } from "../components/Shell";
import type { DiscussionItem } from "../api/types";
import { ApiError, getProject } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { deadlineLabel } from "../lib/time";
import { STATUS_STYLES } from "../theme/status";
import { Breadcrumb } from "../components/Breadcrumb";
import { MilestoneCard } from "../components/MilestoneCard";
import { ChecklistRow } from "../components/ChecklistRow";
import { ReportTimeline } from "../components/ReportTimeline";
import { DiscussionList } from "../components/DiscussionList";
import { DiscussionComposer } from "../components/DiscussionComposer";

export function Project() {
  const { name = "" } = useParams();
  const { me } = useOutletContext<ShellContext>();
  const { data, error, loading } = useApiData(() => getProject(name), [name]);
  const tz = me.display_tz;
  // Discussion items posted this session, appended to the fetched thread; reset on change.
  const [postedDiscussion, setPostedDiscussion] = useState<DiscussionItem[]>([]);
  useEffect(() => setPostedDiscussion([]), [name]);
  // Posting to the discussion needs a thread standing: a supervisor or the developer
  // (admin). A viewer reads it but cannot join — matching the server's 403 gate.
  const canDiscuss =
    me.authenticated &&
    (me.identity?.role === "admin" || me.identity?.role === "supervisor");

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
            <div className="eyebrow block-label">Discussion</div>
            <DiscussionList items={[...data.discussions, ...postedDiscussion]} />
            <DiscussionComposer
              projectName={data.name}
              authorName={me.identity?.name ?? null}
              canDiscuss={canDiscuss}
              onPosted={(d) => setPostedDiscussion((prev) => [...prev, d])}
            />
          </section>
        </div>
      </div>
    </div>
  );
}
