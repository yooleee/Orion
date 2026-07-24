// =============================================================================
// web/src/routes/Tracker.tsx
// -----------------------------------------------------------------------------
// Responsible for: The tracker page ("current focus") — a general checklist that is
//                  NOT a project. A TRACKER header with a done/total + segmented bar, a
//                  legend strip of the full state vocabulary, then grouped checklists
//                  with circular indicators and per-group roll-ups.
// Role in project: A tracker's detail page, part of the projects/KB surface (the standalone
//                  To-dos section retired in KB-surface Inc 1). Reads /api/projects/:name (kind == "tracker");
//                  a 404 renders a clean not-found (existence-hiding carries through).
//                  Visual oracle: design/screenshots/desktop-04-tracker-sepia.png.
// =============================================================================

import { useOutletContext, useParams } from "react-router-dom";
import type { ShellContext } from "../components/Shell";
import type { ChecklistItem, Segments } from "../api/types";
import { ApiError, getProject } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { Breadcrumb } from "../components/Breadcrumb";
import { SegmentedBar } from "../components/ProgressBar";
import { StatusSignal } from "../components/StatusSignal";
import { TrackerGroup } from "../components/TrackerGroup";

/**
 * Tile a checklist into the segmented-bar buckets (overdue/due-soon/remaining/done).
 *
 * Why: the project-detail endpoint ships per-item `state` but not the tracker card's
 * `segments` block, so the page composes it client-side from the states the server already
 * classified — the same compose-from-facts approach the home uses. The four buckets tile
 * the total, so the bar always fills. "remaining" is every open item that is not at risk
 * (not_started or in_progress).
 */
function deriveSegments(checklist: ChecklistItem[]): Segments {
  const seg: Segments = { overdue: 0, due_soon: 0, remaining: 0, done: 0 };
  for (const item of checklist) {
    if (item.done) seg.done += 1;
    else if (item.state === "overdue") seg.overdue += 1;
    else if (item.state === "due_soon") seg.due_soon += 1;
    else seg.remaining += 1;
  }
  return seg;
}

export function Tracker() {
  const { name = "" } = useParams();
  const { me } = useOutletContext<ShellContext>();
  const { data, error, loading } = useApiData(() => getProject(name), [name]);
  const tz = me.display_tz;

  if (loading) return <div className="center-note">Loading…</div>;
  if (error) {
    const msg =
      error instanceof ApiError && error.status === 404
        ? `No tracker “${name}”.`
        : "Could not load this tracker.";
    return <div className="center-note">{msg}</div>;
  }
  if (!data) return null;

  const total = data.checklist.length;
  const done = data.checklist.filter((i) => i.done).length;
  const segments = deriveSegments(data.checklist);

  // Group ordering follows the server's milestone roll-ups; any item whose group has no
  // milestone (or is ungrouped) falls into a trailing "Other" section so nothing is dropped.
  const groupNames = new Set(data.milestones.map((m) => m.group));
  const ungrouped = data.checklist.filter((i) => i.group === null || !groupNames.has(i.group));

  return (
    <div>
      <Breadcrumb items={[{ label: "to-dos", to: "/" }, { label: data.name }]} />

      <div className="tracker-caption-row">
        <span className="tracker-pill">TRACKER</span>
        <span className="tracker-caption">a general checklist, not a project</span>
      </div>
      <h1 className="page-title">{data.name}</h1>

      <div className="tracker-progress">
        <span className="tracker-done-count">
          {done} <span className="tracker-done-sub">/ {total} DONE</span>
        </span>
        <SegmentedBar segments={segments} total={total} />
      </div>

      {/* The full state vocabulary as a legend, so every glyph below is legible. Rendered
          through StatusSignal so each entry is glyph + label (never colour alone). */}
      <div className="legend">
        <StatusSignal state="not_started" />
        <StatusSignal state="in_progress" />
        <StatusSignal state="done" label="submitted" />
        <span className="legend-sep" aria-hidden="true" />
        <StatusSignal state="due_soon" />
        <StatusSignal state="overdue" />
        <StatusSignal state="slipping" />
      </div>

      <div className="tracker-groups">
        {data.milestones.map((m) => (
          <TrackerGroup
            key={m.group}
            title={m.group}
            milestone={m}
            items={data.checklist.filter((i) => i.group === m.group)}
            tz={tz}
          />
        ))}
        {ungrouped.length > 0 && (
          <TrackerGroup title="Other" milestone={null} items={ungrouped} tz={tz} />
        )}
      </div>
    </div>
  );
}
