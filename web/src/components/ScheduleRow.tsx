// =============================================================================
// web/src/components/ScheduleRow.tsx
// -----------------------------------------------------------------------------
// Responsible for: One row in a Scheduling time bucket — a fixed-width relative-time
//                  column (glyph + compact "2d ago" / "in 3d", coloured by urgency),
//                  the item label, an optional slipping marker, and a source-tag pill
//                  (◇ project / ⊟ tracker) showing where the deadline comes from.
// Role in project: The atom of the cross-project Scheduling view. State is rendered
//                  through StatusSignal so it stays legible without colour alone.
// =============================================================================

import type { ScheduleItem } from "../api/types";
import { StatusSignal } from "./StatusSignal";
import { SOURCE_GLYPH } from "../theme/status";
import { scheduleTime } from "../lib/time";

export function ScheduleRow({ item, tz }: { item: ScheduleItem; tz: string }) {
  return (
    <div className="schedule-row">
      {/* Time column: glyph + compact magnitude, coloured by the deadline's state. The
          "later"/upcoming state has no glyph (a neutral relative time), by design. */}
      <span className="schedule-time">
        <StatusSignal state={item.state} label={scheduleTime(item.due_date, tz)} />
      </span>
      <span className="schedule-label">{item.label}</span>
      {item.slipping && <StatusSignal state="slipping" label="slipping" />}
      <span className={`schedule-source ${item.source.kind}`}>
        <span aria-hidden="true">{SOURCE_GLYPH[item.source.kind]}</span> {item.source.name}
      </span>
    </div>
  );
}
