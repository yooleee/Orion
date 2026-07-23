// =============================================================================
// web/src/components/ProjectRow.tsx
// -----------------------------------------------------------------------------
// Responsible for: One project row on the home — name + headline, progress, up to two
//                  forward signals, and a relative last-activity time. Clicks through to
//                  the project page.
// Role in project: The Projects section's repeating unit. It COMPOSES the ≤2 visible
//                  signals from the contract's facts (at_risk / next_due), so the
//                  derivation stays server-side and the presentation choice stays here.
// =============================================================================

import { Link } from "react-router-dom";
import type { ProjectSummary, Status } from "../api/types";
import { ProgressBar } from "./ProgressBar";
import { StatusSignal } from "./StatusSignal";
import { deadlineLabel, relativeTime } from "../lib/time";

interface RowSignal {
  state: Status;
  label: string;
}

/**
 * Compose up to two forward signals for a project row from the contract's facts.
 *
 * Why: the design shows three distinct shapes — "△ N at risk · ◷ due in Xd" (at-risk
 * with a soon deadline), "▲ Xd overdue" alone (the overdue deadline subsumes the count),
 * and "✓ on track" (nothing pressing). This rule reproduces all three: an overdue
 * deadline is the lead signal (the count is redundant beside it); otherwise the at-risk
 * count leads and a due-soon deadline follows; nothing pressing reads "on track".
 */
function rowSignals(p: ProjectSummary, tz: string): RowSignal[] {
  const signals: RowSignal[] = [];
  const nd = p.next_due;
  if (nd && nd.state === "overdue") {
    signals.push({ state: "overdue", label: deadlineLabel(nd.due_date, tz) });
  } else if (p.at_risk > 0) {
    signals.push({ state: "at_risk", label: `${p.at_risk} at risk` });
  }
  if (nd && nd.state === "due_soon") {
    signals.push({ state: "due_soon", label: deadlineLabel(nd.due_date, tz) });
  }
  if (signals.length === 0) {
    signals.push({ state: "on_track", label: "on track" });
  }
  return signals;
}

export function ProjectRow({ project, tz }: { project: ProjectSummary; tz: string }) {
  const hasProgress = project.progress.total > 0;
  const signals = rowSignals(project, tz);

  return (
    <Link to={`/project/${encodeURIComponent(project.name)}`} className="project-row">
      <div className="row-main">
        <div className="row-name">{project.name}</div>
        {/* KB surface (Unit 2): the About line — what the project IS (observed from its
            doc). Distinct from the headline below, which is the latest-report pulse. Shown
            only when the project set an about_file. */}
        {project.about && <div className="row-about">{project.about}</div>}
        {project.headline && <div className="row-headline">{project.headline}</div>}
      </div>

      <div className="row-progress">
        {hasProgress && (
          <>
            <div className="progress-row">
              <span>
                {project.progress.done}/{project.progress.total}
              </span>
              <span>{project.progress.pct}%</span>
            </div>
            <ProgressBar progress={project.progress} />
          </>
        )}
      </div>

      <div className="row-signals">
        {signals.map((s, i) => (
          <StatusSignal key={i} state={s.state} label={s.label} />
        ))}
      </div>

      <div className="row-time">{relativeTime(project.updated_at)}</div>
    </Link>
  );
}
