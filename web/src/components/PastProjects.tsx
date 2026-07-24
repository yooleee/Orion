// =============================================================================
// web/src/components/PastProjects.tsx
// -----------------------------------------------------------------------------
// Responsible for: Home's collapsed "Past projects" section — a count summary that
//                  expands to the finished projects and trackers, each rendered by the
//                  SAME row component the live sections use.
// Role in project: The rendering half of S2.2. A finished project stays fully readable
//                  (its page, its reports, its record) but stops competing for attention
//                  with live work, which is the whole point of declaring it past.
// Why collapsed by default: the KI-34 idiom — density is fixed by making the summary the
//                  default reading and the detail opt-in, the same disclosure pattern
//                  MilestoneGroup uses on the project page. Past work is the case where
//                  that trade is most obviously right: it is reference, not attention.
// Note: it renders BOTH kinds. Anything marked past leaves the live sections, so a past
//       tracker cannot end up sitting unmarked among the active ones.
// =============================================================================

import { useState } from "react";
import type { ProjectSummary, TrackerSummary } from "../api/types";
import { ProjectRow } from "./ProjectRow";
import { TrackerCard } from "./TrackerCard";

export function PastProjects({
  projects,
  trackers,
  tz,
}: {
  projects: ProjectSummary[];
  trackers: TrackerSummary[];
  tz: string;
}) {
  const [open, setOpen] = useState(false);
  const count = projects.length + trackers.length;

  // Nothing past ⇒ no section at all. An empty "Past projects (0)" would be a permanent
  // reminder of an absence, which is worse than saying nothing.
  if (count === 0) return null;

  return (
    <section className="home-section">
      {/* The whole header is the disclosure control: a real <button> so it is
          keyboard-reachable and announces its expanded state to assistive tech. */}
      <button
        type="button"
        className="section-header past-summary"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <h2>
          {/* The caret is decoration — aria-expanded carries the actual state. */}
          <span className={`milestone-caret${open ? " open" : ""}`} aria-hidden="true">
            ▸
          </span>
          Past projects
        </h2>
        <span className="past-count">{count}</span>
        <span className="section-rule" />
      </button>

      {/* Only mounted while open, so a collapsed section costs nothing to render. */}
      {open && (
        <div className="row-stack">
          {projects.map((p) => (
            <ProjectRow key={p.name} project={p} tz={tz} />
          ))}
          {trackers.map((t) => (
            <TrackerCard key={t.name} tracker={t} tz={tz} />
          ))}
        </div>
      )}
    </section>
  );
}
