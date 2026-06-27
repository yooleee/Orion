// =============================================================================
// web/src/components/ContextRail.tsx
// -----------------------------------------------------------------------------
// Responsible for: The report reader's right rail — DETAILS, SENT TO, BUILT FROM, and
//                  CHECKLIST SNAPSHOT cards.
// Role in project: The metadata + snapshot context beside the report body. Fields not
//                  yet carried by the relay degrade gracefully (no recipient roles →
//                  name only; empty source_tags → the BUILT FROM card is omitted).
// =============================================================================

import type { ReportDetail } from "../api/types";
import { ProgressBar } from "./ProgressBar";
import { StatusSignal } from "./StatusSignal";
import { relativeTime } from "../lib/time";

// How many snapshot rows to show before collapsing into "+N more".
const MAX_SNAPSHOT_ROWS = 5;

export function ContextRail({ report }: { report: ReportDetail }) {
  const snap = report.checklist_snapshot;
  const shownRows = snap.rows.slice(0, MAX_SNAPSHOT_ROWS);
  const moreRows = Math.max(0, snap.rows.length - shownRows.length);

  return (
    <aside className="rail">
      {/* DETAILS */}
      <div className="rail-card">
        <div className="eyebrow rail-label">Details</div>
        <dl className="kv">
          <dt>Report</dt>
          <dd>#{report.number}</dd>
          <dt>Generated</dt>
          <dd>{relativeTime(report.generated_at)}</dd>
          <dt>Received</dt>
          <dd>{relativeTime(report.ingested_at)}</dd>
          <dt>Lane</dt>
          <dd>
            <span className="pill pill-accent">{report.lane}</span>
          </dd>
          <dt>Detail</dt>
          <dd>
            <span className="pill">{report.share_level}</span>
          </dd>
          <dt>Version</dt>
          <dd className="kv-mono">Orion {report.orion_version}</dd>
        </dl>
      </div>

      {/* SENT TO */}
      {report.participants.length > 0 && (
        <div className="rail-card">
          <div className="eyebrow rail-label">Sent to</div>
          {report.participants.map((p, i) => (
            <div className="recipient" key={i}>
              <span className="avatar avatar-sm" aria-hidden="true">
                {p.name.charAt(0).toUpperCase()}
              </span>
              <span className="recipient-name">{p.name}</span>
              {p.role && <span className="recipient-role">{p.role}</span>}
            </div>
          ))}
        </div>
      )}

      {/* BUILT FROM — only when the relay carries the source set (gap 4: empty in 4a). */}
      {report.source_tags.length > 0 && (
        <div className="rail-card">
          <div className="eyebrow rail-label">Built from</div>
          <div className="source-chips">
            {report.source_tags.map((tag, i) => (
              <span className="source-chip" key={i}>
                ◦ {tag}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* CHECKLIST SNAPSHOT */}
      {snap.total > 0 && (
        <div className="rail-card">
          <div className="eyebrow rail-label">Checklist snapshot</div>
          <div className="snapshot-head">
            <span className="kv-mono">
              {snap.done}/{snap.total}
            </span>
          </div>
          <ProgressBar progress={{ done: snap.done, total: snap.total, pct: Math.round((snap.done / snap.total) * 100) }} />
          <div className="snapshot-rows">
            {shownRows.map((row, i) => (
              <div className="snapshot-row" key={i}>
                <StatusSignal state={row.state} label="" hideGlyph={false} />
                <span className={`snapshot-text${row.done ? " done" : ""}`}>{row.text}</span>
              </div>
            ))}
            {moreRows > 0 && <div className="snapshot-more">+ {moreRows} more</div>}
          </div>
        </div>
      )}
    </aside>
  );
}
