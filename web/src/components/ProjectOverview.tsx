// =============================================================================
// web/src/components/ProjectOverview.tsx
// -----------------------------------------------------------------------------
// Responsible for: The presentational body of a project's page — the header stats, the
//                  FORWARD LOOK (expandable milestone groups + the trailing "Other"
//                  bucket), the working-agreements and by-contributor blocks, and the
//                  REPORTS timeline. Pure presentation off a ProjectDetail + a timezone.
// Role in project: The single source for "how a project looks", shared by the real
//                  authenticated project page (routes/Project.tsx) and the public Showcase
//                  demo (routes/ShowcaseDemo.tsx). DR1-R U1 extracted it so the two can no
//                  longer drift — the demo previously re-implemented this inline and had
//                  fallen behind (it still showed the pre-KI-34 flat "Live checklist").
// Discussion: the DISCUSSION thread is NOT here — it is the one auth-coupled part (it needs
//             the viewer identity + a live composer), so each caller renders it and passes
//             it in via the `discussion` slot, dropped into the right column after Reports.
// Link mode: threaded straight into ReportTimeline — "app" links reports to /report/:id,
//            "none" opens them via onOpenReport (the router-less demo).
// =============================================================================

import type { ReactNode } from "react";
import type { ProjectDetail } from "../api/types";
import { deadlineLabel, formatDate } from "../lib/time";
import { STATUS_STYLES } from "../theme/status";
import { MilestoneGroup } from "./MilestoneGroup";
import { ChecklistRow } from "./ChecklistRow";
import { DisciplineCard } from "./DisciplineCard";
import { ProgressBar } from "./ProgressBar";
import { ReportTimeline } from "./ReportTimeline";

export function ProjectOverview({
  data,
  tz,
  linkMode = "app",
  onOpenReport,
  discussion,
}: {
  data: ProjectDetail;
  tz: string;
  linkMode?: "app" | "none";
  onOpenReport?: (id: number) => void;
  discussion?: ReactNode;
}) {
  const nextDue = data.stats.next_due;
  // S2.2: a finished project. The relay already nulls stats.next_due, so the stat renders
  // "—" either way — but "—" alone reads as "nothing scheduled", which is a live-project
  // statement. The badge plus the muted treatment say the honest thing instead: this
  // question no longer applies here.
  const isPast = data.lifecycle === "past";

  // KI-34 (Unit 3): items whose group has no milestone roll-up (ungrouped, or a group the
  // server derived no milestone for) still belong somewhere — they trail in an "Other"
  // group rather than being dropped when the flat checklist section retired. Same rule the
  // tracker page uses, so the two pages agree on what "ungrouped" means.
  const milestoneGroups = new Set(data.milestones.map((m) => m.group));
  const ungroupedItems = data.checklist.filter(
    (i) => i.group === null || !milestoneGroups.has(i.group),
  );

  return (
    <div>
      <header className="detail-header">
        <div className="detail-headline">
          <h1 className="page-title">
            {data.name}
            {/* S2.2: the past badge sits with the title, not in the stats — it qualifies
                what the whole page is, rather than being one more measurement. */}
            {isPast && <span className="past-badge">PAST</span>}
          </h1>
          {/* KB surface (Unit 2): the About line under the title — what the project IS,
              observed from its doc, in the page-sub style. */}
          {data.about && <p className="page-sub">{data.about}</p>}
        </div>
        <div className="stat-blocks">
          <div className="stat">
            <div className="stat-label">Progress</div>
            <div className="stat-value">
              {data.stats.progress.done} <span className="stat-sub">/ {data.stats.progress.total}</span>
            </div>
          </div>
          <div className="stat">
            <div className="stat-label">Next due</div>
            <div
              className={`stat-value stat-due${isPast ? " stat-na" : ""}`}
              // Colour by the deadline's state (overdue red vs due-soon amber), not a fixed
              // hue — so an overdue NEXT DUE reads as overdue.
              style={nextDue ? { color: `var(${STATUS_STYLES[nextDue.state].colorVar})` } : undefined}
            >
              {nextDue ? deadlineLabel(nextDue.due_date, tz) : "—"}
            </div>
          </div>
          <div className="stat">
            <div className="stat-label">Reports</div>
            <div className="stat-value">{data.stats.reports_count}</div>
          </div>
        </div>
      </header>

      <div className="detail-grid">
        <div className="detail-left">
          {/* Unit 5: durable project principles observed in the docs, leading the left column
              as context before the progress detail. Every card shows regardless of the
              model's global/project scope; the per-card footer names the source doc. */}
          {data.disciplines && data.disciplines.cards.length > 0 && (
            <section>
              <div className="eyebrow block-label">Working agreements</div>
              <div className="disc-freshness">
                Observed in your docs · updated {formatDate(data.disciplines.updated_at, tz)}
              </div>
              <div className="disc-grid">
                {data.disciplines.cards.map((card, i) => (
                  <DisciplineCard key={`${card.title}-${i}`} card={card} />
                ))}
              </div>
            </section>
          )}

          {/* KI-34 (Unit 3): the forward look and the live checklist are ONE section now.
              Each milestone is an expandable group holding its own items, so the page reads
              as group summaries with detail on demand — instead of milestone cards plus a
              separate flat list of every item. Ungrouped items trail in "Other" so nothing
              is dropped. */}
          {(data.milestones.length > 0 || ungroupedItems.length > 0) && (
            <section>
              <div className="eyebrow block-label">Forward look</div>
              <div className="milestone-stack">
                {data.milestones.map((m) => (
                  <MilestoneGroup
                    key={m.group}
                    title={m.group}
                    milestone={m}
                    items={data.checklist.filter((i) => i.group === m.group)}
                    tz={tz}
                  />
                ))}
                {ungroupedItems.length > 0 && (
                  <MilestoneGroup
                    title="Other"
                    milestone={null}
                    items={ungroupedItems}
                    tz={tz}
                  />
                )}
              </div>
            </section>
          )}

          {/* C3 Inc 2: one card per contributor, shown only when 2+ producers exist — a
              single producer's card would just duplicate the aggregate above. */}
          {data.producer_checklists.length >= 2 && (
            <section>
              <div className="eyebrow block-label">By contributor</div>
              <div className="producer-grid">
                {data.producer_checklists.map((pc) => (
                  <div className="producer-card" key={pc.author_name}>
                    <div className="producer-head">
                      <span className="producer-name">{pc.author_name}</span>
                      <span className="producer-count">
                        {pc.progress.done}/{pc.progress.total}
                      </span>
                    </div>
                    <ProgressBar progress={pc.progress} />
                    <div className="check-list">
                      {pc.items.map((item, i) => (
                        <ChecklistRow
                          key={item.key ?? `${item.text}-${i}`}
                          item={item}
                          tz={tz}
                        />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>

        <div className="detail-right">
          <section>
            <div className="eyebrow block-label">Reports</div>
            <ReportTimeline reports={data.reports} linkMode={linkMode} onOpenReport={onOpenReport} />
          </section>

          {/* The discussion thread is auth-coupled (viewer identity + live composer), so the
              caller owns it and drops it here. The demo passes a read-only thread; the real
              page passes the thread plus a gated composer. */}
          {discussion}
        </div>
      </div>
    </div>
  );
}
