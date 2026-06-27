// =============================================================================
// web/src/components/EmptyState.tsx
// -----------------------------------------------------------------------------
// Responsible for: The first-run / nothing-observed state — a friendly explanation
//                  that content appears once Orion observes activity, with a command
//                  hint. Keeps a fresh install (or a fully-empty scope) intentional.
// Role in project: Shown on the home when there are no projects and no trackers to show.
//                  Reinforces the product line: Orion observes & reframes, never authors.
// =============================================================================

export function EmptyState() {
  return (
    <div className="empty-state">
      <div className="empty-icon" aria-hidden="true">
        ✦
      </div>
      <h1>Welcome to Orion</h1>
      <p>
        Your projects and to-dos appear here once Orion observes activity — git history,
        checklists, and Claude Code sessions it reads and reframes.
      </p>
      <div className="empty-cmd">orion watch ~/projects/my-repo</div>
      <p className="empty-foot">Orion observes &amp; reframes — it never authors your plans.</p>
    </div>
  );
}
