// =============================================================================
// web/src/components/TrackerGroup.tsx
// -----------------------------------------------------------------------------
// Responsible for: One tracker group — a serif group title + rule + a roll-up signal
//                  (at-risk / slipping, the done/total count, and the nearest deadline),
//                  followed by that group's circular checklist rows.
// Role in project: The tracker page's grouped checklist. The roll-up reuses the relay's
//                  per-group milestone derivation (done/total/at_risk/nearest_due/slipping)
//                  so the header agrees with the per-row states below it.
// =============================================================================

import type { ChecklistItem, Milestone, Status } from "../api/types";
import { TrackerRow } from "./TrackerRow";
import { StatusSignal } from "./StatusSignal";
import { deadlineLabel, deadlineState } from "../lib/time";

/**
 * The group roll-up's risk flags: slipping and/or at-risk.
 *
 * Why: unlike the project page's milestone card (which suppresses the deadline when at
 * risk), the tracker header always shows the count and the nearest deadline, so it needs
 * only the urgent FLAGS here. Kept local — it is three lines and the project page's
 * milestoneSignals composes differently (and uses "slipped"), so sharing would couple two
 * genuinely different presentations.
 */
function riskFlags(m: Milestone): Array<{ state: Status; label: string }> {
  const sigs: Array<{ state: Status; label: string }> = [];
  if (m.slipping) sigs.push({ state: "slipping", label: "slipping" });
  if (m.at_risk > 0) sigs.push({ state: "at_risk", label: "at risk" });
  return sigs;
}

export function TrackerGroup({
  title,
  milestone,
  items,
  tz,
}: {
  title: string;
  milestone: Milestone | null;
  items: ChecklistItem[];
  tz: string;
}) {
  return (
    <section className="tracker-group">
      <div className="tracker-group-head">
        <h2 className="tracker-group-title">{title}</h2>
        <span className="section-rule" />
        {milestone && (
          <span className="tracker-group-rollup">
            {riskFlags(milestone).map((s, i) => (
              <StatusSignal key={i} state={s.state} label={s.label} />
            ))}
            <span className="tracker-group-count">
              {milestone.done}/{milestone.total}
            </span>
            {milestone.nearest_due && (
              <>
                <span className="rollup-sep">·</span>
                <StatusSignal
                  state={deadlineState(milestone.nearest_due, tz)}
                  label={deadlineLabel(milestone.nearest_due, tz)}
                />
              </>
            )}
          </span>
        )}
      </div>
      <div className="tracker-rows">
        {items.map((item, i) => (
          <TrackerRow key={item.key ?? `${item.text}-${i}`} item={item} tz={tz} />
        ))}
      </div>
    </section>
  );
}
