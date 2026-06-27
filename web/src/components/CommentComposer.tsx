// =============================================================================
// web/src/components/CommentComposer.tsx
// -----------------------------------------------------------------------------
// Responsible for: The comment composer UI — present and on-design, but INERT in 4a.
// Role in project: 4a is the read-only slice (decision: render the composer, defer the
//                  write path to its own slice so the SPA write + CSRF/auth gets a
//                  focused review). So this shows "Commenting as {name}", a textarea, and
//                  a disabled Post button with an honest caption — no submit handler.
// =============================================================================

interface CommentComposerProps {
  /** The signed-in viewer's name, or null on an open/anonymous relay. */
  authorName: string | null;
}

export function CommentComposer({ authorName }: CommentComposerProps) {
  return (
    <div className="composer">
      <div className="composer-as">
        {authorName ? `Commenting as ${authorName}` : "Sign in to comment"}
      </div>
      <textarea
        className="composer-input"
        placeholder="Add a comment…"
        rows={3}
        disabled
      />
      <div className="composer-actions">
        <span className="composer-note">Commenting arrives in a later update.</span>
        <button type="button" className="btn-primary btn-inline" disabled>
          Post comment
        </button>
      </div>
    </div>
  );
}
