// =============================================================================
// web/src/components/ReportTimeline.tsx
// -----------------------------------------------------------------------------
// Responsible for: The REPORTS timeline on the project page — a vertical dot +
//                  connector list, newest first; each entry opens the report.
// Role in project: The project's history of progress updates. The newest dot is the
//                  accent; older dots are faint (the design's recency cue).
// Link mode: "app" (default) links each entry to the authed /report/:id route; "none"
//            renders a button that calls onOpenReport instead, for the public Showcase
//            demo — which has no router and must never link into an authenticated route.
//            DR1-R U1 gave the demo this shared timeline (it used to hand-roll its own).
// =============================================================================

import { Link } from "react-router-dom";
import type { ReportSummary } from "../api/types";
import { relativeTime } from "../lib/time";
import { AgentBadge } from "./AgentBadge";

export function ReportTimeline({
  reports,
  linkMode = "app",
  onOpenReport,
}: {
  reports: ReportSummary[];
  linkMode?: "app" | "none";
  onOpenReport?: (id: number) => void;
}) {
  if (reports.length === 0) {
    return <div className="comments-empty">No reports yet.</div>;
  }
  return (
    <div className="timeline">
      {reports.map((r, i) => {
        // The entry's inner markup is identical in both modes — only the wrapper element
        // (route Link vs. plain button) differs, so it lives in one place.
        const body = (
          <>
            {/* The most-recent entry (index 0) gets the accent dot; older are faint. */}
            <span className={`timeline-dot${i === 0 ? " latest" : ""}`} aria-hidden="true" />
            <div className="timeline-body">
              <div className="timeline-title">{r.title || `Report #${r.number}`}</div>
              <div className="timeline-meta">
                #{r.number} · {relativeTime(r.generated_at)}
                {/* C3 Inc 2: show the pushing producer only when attributed — a legacy/older
                    report has author_name null and renders exactly as before. */}
                {r.author_name && <> · {r.author_name}</>}
                {/* Unit 4a: an agent's push is badged and names the human it acted for. */}
                <AgentBadge kind={r.author_kind} operatedBy={r.operated_by_name} />
                {r.source_tags.length > 0 && <> · {r.source_tags.join(" ")}</>}
              </div>
            </div>
          </>
        );

        // "none" mode (the public demo): open by internal state, not a route. The
        // demo-report-entry class resets the button so it reads like the link entries.
        return linkMode === "none" ? (
          <button
            type="button"
            className="timeline-entry demo-report-entry"
            key={r.id}
            onClick={() => onOpenReport?.(r.id)}
          >
            {body}
          </button>
        ) : (
          <Link to={`/report/${r.id}`} className="timeline-entry" key={r.id}>
            {body}
          </Link>
        );
      })}
    </div>
  );
}
