// =============================================================================
// web/src/routes/Project.tsx
// -----------------------------------------------------------------------------
// Responsible for: One project's full page — header stats, the FORWARD LOOK (expandable
//                  milestone groups, each holding its own checklist items), the REPORTS
//                  timeline, and the project-level DISCUSSION thread (the one conversation
//                  surface here). Two-column layout.
// Note: the separate flat "Live checklist" section retired in KI-34 (KB surface Unit 3) —
//       it repeated every item the milestone cards already summarized. Items now nest in
//       their milestone group (ungrouped ones trail in "Other"), so nothing is lost.
// Note: this project-level Discussion thread (E2 Inc 5) is the ONE conversation surface —
//       per-report comments were retired outright in KI-28 Stage 2 (two near-identical
//       surfaces doing one job, consolidated to Discussion).
// Role in project: Everything observed about a project. Reads /api/projects/:name; a
//                  404 renders a clean not-found (existence-hiding carries through).
// =============================================================================

import { useEffect, useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";
import type { ShellContext } from "../components/Shell";
import type { DiscussionItem } from "../api/types";
import { ApiError, getProject } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { Breadcrumb } from "../components/Breadcrumb";
import { ProjectOverview } from "../components/ProjectOverview";
import { DiscussionList } from "../components/DiscussionList";
import { DiscussionComposer } from "../components/DiscussionComposer";

export function Project() {
  const { name = "" } = useParams();
  const { me } = useOutletContext<ShellContext>();
  const { data, error, loading } = useApiData(() => getProject(name), [name]);
  const tz = me.display_tz;
  // Discussion items posted this session, appended to the fetched thread; reset on change.
  const [postedDiscussion, setPostedDiscussion] = useState<DiscussionItem[]>([]);
  useEffect(() => setPostedDiscussion([]), [name]);
  // Posting to the discussion needs a thread standing: a supervisor or the developer
  // (admin). A viewer reads it but cannot join — matching the server's 403 gate.
  const canDiscuss =
    me.authenticated &&
    (me.identity?.role === "admin" || me.identity?.role === "supervisor");

  if (loading) return <div className="center-note">Loading…</div>;
  if (error) {
    const msg = error instanceof ApiError && error.status === 404 ? `No project “${name}”.` : "Could not load this project.";
    return <div className="center-note">{msg}</div>;
  }
  if (!data) return null;

  return (
    <div>
      <Breadcrumb items={[{ label: "projects", to: "/" }, { label: data.name }]} />

      {/* The presentational overview (header + forward look + reports) is shared with the
          public Showcase demo via ProjectOverview (DR1-R U1). The Discussion thread is the
          one auth-coupled part — its live composer needs the viewer identity — so it stays
          here and is passed into the overview's right column as a slot. */}
      <ProjectOverview
        data={data}
        tz={tz}
        discussion={
          <section>
            <div className="eyebrow block-label">Discussion</div>
            <DiscussionList items={[...data.discussions, ...postedDiscussion]} />
            <DiscussionComposer
              projectName={data.name}
              authorName={me.identity?.name ?? null}
              canDiscuss={canDiscuss}
              onPosted={(d) => setPostedDiscussion((prev) => [...prev, d])}
            />
          </section>
        }
      />
    </div>
  );
}
