// =============================================================================
// web/src/components/ReportTimeline.tsx
// -----------------------------------------------------------------------------
// Responsible for: The REPORTS timeline on the project page — a vertical dot +
//                  connector list, newest first; each entry links to the report.
// Role in project: The project's history of progress updates. The newest dot is the
//                  accent; older dots are faint (the design's recency cue).
// =============================================================================

import { Link } from "react-router-dom";
import type { ReportSummary } from "../api/types";
import { relativeTime } from "../lib/time";
import { AgentBadge } from "./AgentBadge";

export function ReportTimeline({ reports }: { reports: ReportSummary[] }) {
  if (reports.length === 0) {
    return <div className="comments-empty">No reports yet.</div>;
  }
  return (
    <div className="timeline">
      {reports.map((r, i) => (
        <Link to={`/report/${r.id}`} className="timeline-entry" key={r.id}>
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
        </Link>
      ))}
    </div>
  );
}
