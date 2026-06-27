// =============================================================================
// web/src/components/TrackerRow.tsx
// -----------------------------------------------------------------------------
// Responsible for: One tracker checklist row — a CIRCULAR status indicator (bordered
//                  = not started; conic arc = in progress; --over border = overdue;
//                  filled ✓ = done), the clean item label, and status pill(s).
// Role in project: The tracker page's row. Circular indicators (vs the project page's
//                  square checkboxes in ChecklistRow) encode the projects-vs-todos IA.
//                  The in-progress arc is driven by the raw `status` field (E2 Inc 4,
//                  gap 8) so it reads independently of the single derived `state`.
// =============================================================================

import type { ChecklistItem, ItemStatus, Status } from "../api/types";
import { StatusSignal } from "./StatusSignal";
import { deadlineLabel } from "../lib/time";

/**
 * Map the producer's raw item status to a status pill (presentation Status + label).
 *
 * Args:
 *   status: the raw observed status, or null for a status-less item.
 *   done: whether the item is finished.
 *
 * Returns: the pill to render, or null when none adds information.
 *
 * Why: the tracker is status-aware, so an OPEN item shows in-progress vs not-started (the
 * gap-8 distinction) and a DONE item shows HOW it finished (submitted/closed) when known.
 * A plain done item adds no pill — the filled circle already says "done" — and a
 * status-less open item (e.g. a table to-do row) defaults to not-started.
 */
function statusPill(status: ItemStatus | null, done: boolean): { state: Status; label: string } | null {
  if (done) {
    if (status === "submitted") return { state: "done", label: "submitted" };
    if (status === "closed") return { state: "done", label: "closed" };
    return null; // plain done — the filled circle is the signal, a "done" pill is redundant
  }
  if (status === "in_progress") return { state: "in_progress", label: "in progress" };
  return { state: "not_started", label: "not started" };
}

export function TrackerRow({ item, tz }: { item: ChecklistItem; tz: string }) {
  // The circle's two visual aspects compose independently: the FILL comes from done /
  // in_progress, the BORDER colour from overdue — an item can be both (in-progress AND
  // overdue), so the modifiers are additive rather than mutually exclusive.
  const inProgress = !item.done && item.status === "in_progress";
  const overdue = item.state === "overdue";
  // The clean title with the embedded status stripped: `key` is the bare title for
  // application items, and `text` is already clean for status-less rows — so `key ?? text`
  // is the display label with no parsing (E2 Inc 4 decision).
  const label = item.key ?? item.text;

  const signals: Array<{ state: Status; label: string }> = [];
  const pill = statusPill(item.status, item.done);
  if (pill) signals.push(pill);
  if (!item.done) {
    if (item.slipping) signals.push({ state: "slipping", label: "slipping" });
    if ((item.state === "overdue" || item.state === "due_soon") && item.due_date) {
      signals.push({ state: item.state, label: deadlineLabel(item.due_date, tz) });
    }
  }

  const circleClass =
    "check-circle" +
    (item.done ? " done" : "") +
    (inProgress ? " in-progress" : "") +
    (overdue ? " overdue" : "");

  return (
    <div className="tracker-row">
      <span className={circleClass} aria-hidden="true">
        {item.done ? "✓" : ""}
      </span>
      <span className={`tracker-label${item.done ? " done" : ""}`}>{label}</span>
      {signals.length > 0 && (
        <span className="tracker-signals">
          {signals.map((s, i) => (
            <StatusSignal key={i} state={s.state} label={s.label} />
          ))}
        </span>
      )}
    </div>
  );
}
