// =============================================================================
// web/src/routes/Search.tsx
// -----------------------------------------------------------------------------
// Responsible for: The Search page (S2.3 / KB Inc 3) — the one search box + the
//                  results view over GET /api/search: hits grouped by project, the
//                  report and discussion classes kept distinct within each group,
//                  matches highlighted client-side.
// Role in project: The cross-project retrieval surface of the KB — Orion remembers
//                  what it observed, and this is where that memory is reachable. The
//                  URL (?q=) is the single source of query state, so a search is
//                  shareable and back-button-correct.
// Security: snippets/titles arrive as RAW plain text (the server does not escape);
//           they are rendered only as React text children via highlightTerms —
//           escape-before-highlight, pinned by highlight.test.tsx.
// =============================================================================

import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { SearchResults } from "../api/types";
import { getSearch } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { highlightTerms } from "../lib/highlight";
import { relativeTime } from "../lib/time";

// The contract's minimum (docs/dashboard-api-contract.md): under 2 stripped chars the
// server answers 400, so the page never calls the API below it — the prompt state is
// the client-side mirror of that rule, not a separate opinion.
const MIN_QUERY_CHARS = 2;

/** One project's slice of the results: its report hits and its discussion hits, in the
 *  order the server returned them (newest first within each class). */
interface ProjectGroup {
  project: string;
  reports: SearchResults["reports"]["hits"];
  discussions: SearchResults["discussions"]["hits"];
}

/**
 * Group both hit classes by project, preserving first-appearance order.
 *
 * Args:
 *   data: the /api/search response (flat, newest-first per class).
 *
 * Returns: one ProjectGroup per project that has at least one hit, ordered by where
 * the project first appears (reports scanned before discussions, both newest-first).
 *
 * Why: the server ships flat data and the client owns presentation (the contract's
 * split). Grouping by project is the cross-project view's value — a reader scans
 * "where did this come up", then drills in. First-appearance order keeps the freshest
 * project on top without inventing a cross-class ranking the server never promised.
 */
function groupByProject(data: SearchResults): ProjectGroup[] {
  const groups = new Map<string, ProjectGroup>();
  const groupFor = (project: string): ProjectGroup => {
    let g = groups.get(project);
    if (!g) {
      g = { project, reports: [], discussions: [] };
      groups.set(project, g);
    }
    return g;
  };
  for (const hit of data.reports.hits) groupFor(hit.project).reports.push(hit);
  for (const hit of data.discussions.hits) groupFor(hit.project).discussions.push(hit);
  return [...groups.values()];
}

export function Search() {
  const [params, setParams] = useSearchParams();
  const q = (params.get("q") ?? "").trim();
  const active = q.length >= MIN_QUERY_CHARS;
  const terms = active ? q.split(/\s+/) : [];

  // The box edits a local draft; only submitting writes ?q= (and so triggers a fetch).
  // Synced FROM the URL so back/forward and a shared link fill the box correctly.
  const [draft, setDraft] = useState(q);
  useEffect(() => setDraft(q), [q]);

  // Below the minimum nothing is fetched: the loader resolves to null and the page
  // shows the prompt state. Distinct states stay distinct — prompt (not asked),
  // loading, error, and "asked, zero matches" each render differently.
  const { data, error, loading } = useApiData<SearchResults | null>(
    () => (active ? getSearch(q) : Promise.resolve(null)),
    [q],
  );

  const groups = data ? groupByProject(data) : [];
  const total = data
    ? data.reports.hits.length + data.discussions.hits.length
    : 0;

  return (
    <div>
      <div className="page-header">
        <div className="eyebrow">Knowledge base</div>
        <h1 className="page-title">Search</h1>
        <p className="page-sub">
          Search everything Orion has observed — report bodies and discussion threads,
          across all your projects.
        </p>
      </div>

      <form
        className="search-form"
        onSubmit={(e) => {
          e.preventDefault();
          const next = draft.trim();
          // Writing ?q= is the ONLY trigger; an empty submit clears back to the prompt.
          setParams(next ? { q: next } : {});
        }}
      >
        <input
          className="field search-field"
          type="search"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Search reports and discussions…"
          aria-label="Search reports and discussions"
        />
        <button className="btn-primary" type="submit">
          Search
        </button>
      </form>

      {!active && (
        <div className="center-note">
          Type at least two characters and search — every word must match.
        </div>
      )}
      {active && loading && <div className="center-note">Searching…</div>}
      {active && error && (
        <div className="center-note">Could not search. Is the relay reachable?</div>
      )}

      {active && data && total === 0 && (
        <div className="center-note">No matches for “{data.query}”.</div>
      )}

      {active && data && total > 0 && (
        <>
          {groups.map((group) => (
            <section className="home-section" key={group.project}>
              <div className="section-header">
                <h2>
                  <Link to={`/project/${encodeURIComponent(group.project)}`}>
                    {group.project}
                  </Link>
                </h2>
                <span className="section-rule" />
              </div>

              {/* The two classes stay visually distinct — a narrated report and a
                  conversation turn are different things (the wire keeps them apart;
                  so does the page). */}
              {group.reports.length > 0 && (
                <div className="search-class">
                  <div className="eyebrow search-class-label">Reports</div>
                  <div className="row-stack">
                    {group.reports.map((hit) => (
                      <Link
                        key={hit.id}
                        to={`/report/${hit.id}`}
                        className="project-row search-hit"
                      >
                        <div className="row-main">
                          <div className="row-name">
                            {highlightTerms(hit.title, terms)}
                          </div>
                          <div className="row-headline">
                            {highlightTerms(hit.snippet, terms)}
                          </div>
                        </div>
                        <div className="row-time">{relativeTime(hit.generated_at)}</div>
                      </Link>
                    ))}
                  </div>
                </div>
              )}

              {group.discussions.length > 0 && (
                <div className="search-class">
                  <div className="eyebrow search-class-label">Discussion</div>
                  <div className="row-stack">
                    {group.discussions.map((hit) => (
                      <Link
                        key={hit.id}
                        to={`/project/${encodeURIComponent(hit.project)}#discussion`}
                        className="project-row search-hit"
                      >
                        <div className="row-main">
                          <div className="row-name">
                            {hit.author_name}
                            <span className="search-role"> · {hit.role}</span>
                          </div>
                          <div className="row-headline">
                            {highlightTerms(hit.snippet, terms)}
                          </div>
                        </div>
                        <div className="row-time">{relativeTime(hit.created_at)}</div>
                      </Link>
                    ))}
                  </div>
                </div>
              )}
            </section>
          ))}

          {/* The cap is never silent (the wire's `capped` flag exists for exactly this
              note): say what was cut and how to see it, per class. */}
          {data.reports.capped && (
            <div className="center-note">
              Showing the newest 50 report matches — narrow the search to reach older ones.
            </div>
          )}
          {data.discussions.capped && (
            <div className="center-note">
              Showing the newest 50 discussion matches — narrow the search to reach older
              ones.
            </div>
          )}
        </>
      )}
    </div>
  );
}
