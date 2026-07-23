// =============================================================================
// web/src/components/MilestoneGroup.tsx
// -----------------------------------------------------------------------------
// Responsible for: One EXPANDABLE milestone group on the project page — the milestone
//                  roll-up (title, status signals, progress bar, done/total) as an
//                  always-visible summary, with that group's checklist items nested
//                  inside, revealed on click.
// Role in project: The KI-34 regroup (KB surface Inc 1, Unit 3). The project page used to
//                  show milestone cards AND a separate flat full-length checklist — every
//                  item, always, duplicating what the cards summarized. This folds the
//                  items INTO their milestone, so the page reads as a scannable set of
//                  group summaries that a reader expands only where they want detail.
// Note: `milestone` is nullable so items belonging to NO milestone group still get a home
//       (the trailing "Other" group), exactly as the tracker page does — nothing is dropped.
// =============================================================================

import { useState } from "react";
import type { ChecklistItem, Milestone } from "../api/types";
import { ChecklistRow } from "./ChecklistRow";
import { ProgressBar } from "./ProgressBar";
import { StatusSignal } from "./StatusSignal";
import { milestoneSignals } from "./MilestoneCard";

export function MilestoneGroup({
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
  // Collapsed by default: the density KI-34 records comes from showing every item at once,
  // so the summary is the default reading and detail is opt-in. Local UI state (not a
  // route) — each group opens independently.
  const [open, setOpen] = useState(false);

  // The roll-up counts come from the server-derived milestone when there is one. For the
  // ungrouped "Other" bucket there is no server-side group, so we count the items directly
  // (composing from facts the server already classified, like the tracker page's segments).
  const done = milestone ? milestone.done : items.filter((i) => i.done).length;
  const total = milestone ? milestone.total : items.length;
  const atRisk = milestone !== null && (milestone.at_risk > 0 || milestone.slipping);

  return (
    <div className={`milestone-card${atRisk ? " at-risk" : ""}`}>
      {/* The whole summary is the disclosure control: a real <button> so it is
          keyboard-reachable and announces its expanded state to assistive tech. */}
      <button
        type="button"
        className="milestone-summary"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <div className="milestone-head">
          <span className="milestone-title">
            {/* A rotating caret marks expandability; the state is carried by aria-expanded,
                so the glyph is decorative only. */}
            <span className={`milestone-caret${open ? " open" : ""}`} aria-hidden="true">
              ▸
            </span>
            {title}
          </span>
          <span className="milestone-signals">
            {/* Risk/deadline signals only exist for a real milestone; the ungrouped bucket
                shows its count alone rather than inventing a state the server never derived. */}
            {milestone &&
              milestoneSignals(milestone, tz).map((s, i) => (
                <StatusSignal key={i} state={s.state} label={s.label} />
              ))}
          </span>
        </div>
        <div className="milestone-bar">
          <ProgressBar
            progress={{ done, total, pct: total ? Math.round((done / total) * 100) : null }}
          />
          <span className="milestone-count">
            {done}/{total}
          </span>
        </div>
      </button>

      {/* The nested detail: this group's items, in the order the server sent them. Only
          mounted while open — a collapsed group costs nothing to render. */}
      {open && (
        <div className="milestone-items">
          {items.length > 0 ? (
            items.map((item, i) => (
              <ChecklistRow key={item.key ?? `${item.text}-${i}`} item={item} tz={tz} />
            ))
          ) : (
            // A milestone can roll up counts with no items on THIS page's checklist (e.g.
            // a scoped read). Say so rather than rendering an empty box.
            <div className="milestone-empty">No items in this group.</div>
          )}
        </div>
      )}
    </div>
  );
}
