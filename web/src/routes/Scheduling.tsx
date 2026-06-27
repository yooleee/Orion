// =============================================================================
// web/src/routes/Scheduling.tsx
// -----------------------------------------------------------------------------
// Responsible for: The Scheduling page — every open, dated deadline across all scoped
//                  projects + the tracker, gathered into three time buckets (OVERDUE /
//                  THIS WEEK / LATER) with a summary chip row above them.
// Role in project: The cross-project "by when" lens. Reads /api/scheduling (a pure
//                  read-only aggregation; the server already scope-filters). Visual
//                  oracle: design/screenshots/desktop-05-scheduling-sepia.png.
// =============================================================================

import { useOutletContext } from "react-router-dom";
import type { ShellContext } from "../components/Shell";
import { getScheduling } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { StatusSignal } from "../components/StatusSignal";
import { ScheduleRow } from "../components/ScheduleRow";

// The three buckets in display order, with the dot colour for each bucket header.
const BUCKETS = [
  { key: "overdue", label: "Overdue", dot: "--over" },
  { key: "this_week", label: "This week", dot: "--due" },
  { key: "later", label: "Later", dot: "--tfaint" },
] as const;

export function Scheduling() {
  const { me } = useOutletContext<ShellContext>();
  const { data, error, loading } = useApiData(getScheduling, []);
  const tz = me.display_tz;

  if (loading) return <div className="center-note">Loading…</div>;
  if (error) return <div className="center-note">Could not load the schedule.</div>;
  if (!data) return null;

  const { summary, buckets } = data;
  const total = buckets.overdue.length + buckets.this_week.length + buckets.later.length;

  return (
    <div>
      <div className="page-header">
        <div className="eyebrow">Forward schedule</div>
        <h1 className="page-title">Scheduling</h1>
        <p className="page-sub">
          Every deadline across your projects and to-dos, gathered into one time-ordered view —
          the same items, seen by when they&rsquo;re due.
        </p>
      </div>

      {total === 0 ? (
        <div className="center-note">Nothing scheduled — no open, dated deadlines yet.</div>
      ) : (
        <>
          {/* Summary chips: tinted by state. Each appears only when its count is non-zero. */}
          <div className="schedule-summary">
            {summary.overdue > 0 && (
              <span className="schedule-chip" style={{ background: "var(--over-bg)" }}>
                <StatusSignal state="overdue" label={`${summary.overdue} overdue`} />
              </span>
            )}
            {summary.due_this_week > 0 && (
              <span className="schedule-chip" style={{ background: "var(--due-bg)" }}>
                <StatusSignal state="due_soon" label={`${summary.due_this_week} due this week`} />
              </span>
            )}
            {summary.slipping > 0 && (
              <span className="schedule-chip" style={{ background: "var(--slip-bg)" }}>
                <StatusSignal state="slipping" label={`${summary.slipping} slipping`} />
              </span>
            )}
          </div>

          {BUCKETS.map(({ key, label, dot }) => {
            const rows = buckets[key];
            if (rows.length === 0) return null; // empty buckets are omitted entirely
            return (
              <section className="schedule-bucket" key={key}>
                <div className="schedule-bucket-head">
                  <span className="schedule-dot" style={{ background: `var(${dot})` }} aria-hidden="true" />
                  <span className="schedule-bucket-label">{label}</span>
                  <span className="section-rule" />
                </div>
                <div className="schedule-rows">
                  {rows.map((item, i) => (
                    <ScheduleRow key={`${item.source.name}-${item.label}-${i}`} item={item} tz={tz} />
                  ))}
                </div>
              </section>
            );
          })}
        </>
      )}
    </div>
  );
}
