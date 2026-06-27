// =============================================================================
// web/src/components/ChecklistRow.tsx
// -----------------------------------------------------------------------------
// Responsible for: One LIVE CHECKLIST row — a square checkbox (filled accent ✓ when
//                  done; --over border when overdue), the item label (struck through
//                  when done), and optional right-aligned status signal(s).
// Role in project: The project page's checklist. Square checkboxes (vs the tracker's
//                  circular indicators) encode the projects-vs-todos IA.
// =============================================================================

import type { ChecklistItem, Status } from "../api/types";
import { StatusSignal } from "./StatusSignal";
import { deadlineLabel } from "../lib/time";

export function ChecklistRow({ item, tz }: { item: ChecklistItem; tz: string }) {
  const overdue = item.state === "overdue";
  // Right-side signal(s): slipping marker and/or a deadline chip — only for open items
  // (a done item carries no forward signal).
  const signals: Array<{ state: Status; label: string }> = [];
  if (!item.done) {
    if (item.slipping) signals.push({ state: "slipping", label: "slipping" });
    if ((item.state === "overdue" || item.state === "due_soon") && item.due_date) {
      signals.push({ state: item.state, label: deadlineLabel(item.due_date, tz) });
    }
  }

  return (
    <div className="check-row">
      <span
        className={`checkbox${item.done ? " done" : ""}${overdue ? " overdue" : ""}`}
        aria-hidden="true"
      >
        {item.done ? "✓" : ""}
      </span>
      <span className={`check-label${item.done ? " done" : ""}`}>{item.text}</span>
      {signals.length > 0 && (
        <span className="check-signals">
          {signals.map((s, i) => (
            <StatusSignal key={i} state={s.state} label={s.label} />
          ))}
        </span>
      )}
    </div>
  );
}
