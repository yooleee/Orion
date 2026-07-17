// =============================================================================
// web/src/components/MilestoneCard.tsx
// -----------------------------------------------------------------------------
// Responsible for: One FORWARD LOOK milestone card — group title + status signal(s),
//                  a progress bar + done/total. An at-risk milestone flags with an
//                  --over border (the design's at-risk treatment).
// Role in project: The project page's forward-looking block, one card per milestone
//                  group (derived server-side; this only presents it).
// =============================================================================

import type { Milestone, Status } from "../api/types";
import { ProgressBar } from "./ProgressBar";
import { StatusSignal } from "./StatusSignal";
import { deadlineLabel, deadlineState } from "../lib/time";

/**
 * Compose a milestone's status signals: slipping and/or at-risk lead (an at-risk
 * milestone shows both ↝ slipped and △ at risk when applicable); otherwise the nearest
 * deadline, else not-started / on-track. Up to two, matching the design's milestone states.
 */
function milestoneSignals(m: Milestone, tz: string): Array<{ state: Status; label: string }> {
  const sigs: Array<{ state: Status; label: string }> = [];
  // E1.2 Unit 5: surface the count only when more than one item slips ("2 slipped"). A
  // single slip stays the bare "slipped" label (and zero shows no slip signal at all), so
  // the common cases render exactly as before and only a multi-slip group gains the number.
  if (m.slipping) {
    sigs.push({
      state: "slipping",
      label: m.slipping_count > 1 ? `${m.slipping_count} slipped` : "slipped",
    });
  }
  if (m.at_risk > 0) sigs.push({ state: "at_risk", label: "at risk" });
  if (sigs.length === 0) {
    if (m.nearest_due) {
      sigs.push({ state: deadlineState(m.nearest_due, tz), label: deadlineLabel(m.nearest_due, tz) });
    } else if (m.done === 0) {
      sigs.push({ state: "not_started", label: "not started" });
    } else if (m.done >= m.total) {
      sigs.push({ state: "on_track", label: "done" });
    } else {
      sigs.push({ state: "on_track", label: "on track" });
    }
  }
  return sigs;
}

export function MilestoneCard({ milestone, tz }: { milestone: Milestone; tz: string }) {
  const atRisk = milestone.at_risk > 0 || milestone.slipping;
  return (
    <div className={`milestone-card${atRisk ? " at-risk" : ""}`}>
      <div className="milestone-head">
        <span className="milestone-title">{milestone.group}</span>
        <span className="milestone-signals">
          {milestoneSignals(milestone, tz).map((s, i) => (
            <StatusSignal key={i} state={s.state} label={s.label} />
          ))}
        </span>
      </div>
      <div className="milestone-bar">
        <ProgressBar progress={{ done: milestone.done, total: milestone.total, pct: milestone.total ? Math.round((milestone.done / milestone.total) * 100) : null }} />
        <span className="milestone-count">
          {milestone.done}/{milestone.total}
        </span>
      </div>
    </div>
  );
}
