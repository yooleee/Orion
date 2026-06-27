// =============================================================================
// web/src/components/TrackerCard.tsx
// -----------------------------------------------------------------------------
// Responsible for: The To-dos section's tracker card — styled DELIBERATELY unlike a
//                  project row (accent left-border, TRACKER pill, segmented bar, forward
//                  chips) so the projects-vs-todos IA reads at a glance.
// Role in project: The home's representation of a general checklist (e.g. applications).
//                  Clicks through to the tracker page.
// =============================================================================

import { Link } from "react-router-dom";
import type { TrackerSummary } from "../api/types";
import { SegmentedBar } from "./ProgressBar";
import { StatusSignal } from "./StatusSignal";
import { deadlineLabel } from "../lib/time";

// How many forward-signal chips to show before collapsing the rest into "+N more".
const MAX_CHIPS = 2;

export function TrackerCard({ tracker, tz }: { tracker: TrackerSummary; tz: string }) {
  const chips = tracker.at_risk_items.slice(0, MAX_CHIPS);
  // "+N more →" covers the items not surfaced as a chip (the rest of the list to review).
  const moreCount = Math.max(0, tracker.item_count - chips.length);

  return (
    <Link to={`/tracker/${encodeURIComponent(tracker.name)}`} className="tracker-card">
      <div className="tracker-head">
        <span className="tracker-pill">TRACKER</span>
        <span className="tracker-name">{tracker.name}</span>
        <span className="tracker-count">{tracker.item_count} items</span>
        <span className="tracker-done">
          {tracker.progress.done}/{tracker.progress.total} done
        </span>
      </div>

      <SegmentedBar segments={tracker.segments} total={tracker.item_count} />

      <div className="tracker-chips">
        {chips.map((c, i) => (
          <span className="chip" key={i}>
            <StatusSignal
              state={c.state}
              label={`${c.label} · ${deadlineLabel(c.due_date, tz)}`}
            />
          </span>
        ))}
        {moreCount > 0 && <span className="more-link">+ {moreCount} more →</span>}
      </div>
    </Link>
  );
}
