// =============================================================================
// web/src/components/CommentComposer.tsx
// -----------------------------------------------------------------------------
// Responsible for: The comment composer — the ONLY user-authored write in the
//                  dashboard. A controlled textarea + post button that writes via
//                  the cookie-authed JSON endpoint and hands the created comment back
//                  to the parent to append.
// Role in project: Wired in the comment-writes slice (KI-23). It is enabled only when
//                  the viewer can comment (logged in, or an open relay) and a report
//                  exists to attach to; otherwise it shows a clear disabled state.
// Security: the body is sent as plain JSON and rendered back as an inert React text
//           node (CommentList), never dangerouslySetInnerHTML. The server attributes
//           the comment to the session identity (the typed-in name cannot spoof it).
// =============================================================================

import { useState } from "react";
import type { Comment } from "../api/types";
import { ApiError, postComment } from "../api/client";

interface CommentComposerProps {
  /** The report to attach to, or null when there is none yet (composer disabled). */
  reportId: number | null;
  /** The signed-in viewer's name, or null on an open/anonymous relay. */
  authorName: string | null;
  /** Whether this viewer may comment (authenticated, or an open relay). */
  canComment: boolean;
  /** Called with the created comment on a successful post, so the parent appends it. */
  onPosted: (comment: Comment) => void;
}

export function CommentComposer({ reportId, authorName, canComment, onPosted }: CommentComposerProps) {
  const [body, setBody] = useState("");
  const [posting, setPosting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A clear, non-interactive state when commenting isn't possible: not signed in (gated
  // relay), or no report to attach to (a project with no reports yet).
  const disabledReason = !canComment
    ? "Sign in to comment"
    : reportId === null
      ? "No reports yet to comment on"
      : null;

  if (disabledReason !== null) {
    return (
      <div className="composer">
        <div className="composer-as">{disabledReason}</div>
        <textarea className="composer-input" placeholder="Add a comment…" rows={3} disabled />
        <div className="composer-actions">
          <span className="composer-note">
            {canComment ? "Comments attach to a report." : "Sign in to join the conversation."}
          </span>
          <button type="button" className="btn-primary btn-inline" disabled>
            Post comment
          </button>
        </div>
      </div>
    );
  }

  const trimmed = body.trim();

  async function submit() {
    if (!trimmed || posting || reportId === null) return;
    setPosting(true);
    setError(null);
    try {
      const created = await postComment(reportId, trimmed);
      onPosted(created);
      setBody("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not post your comment.");
    } finally {
      setPosting(false);
    }
  }

  return (
    <div className="composer">
      <div className="composer-as">
        {authorName ? `Commenting as ${authorName}` : "Commenting as guest"}
      </div>
      <textarea
        className="composer-input"
        placeholder="Add a comment…"
        rows={3}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        disabled={posting}
      />
      <div className="composer-actions">
        <span className={`composer-note${error ? " composer-error" : ""}`}>
          {error ?? (posting ? "Posting…" : "")}
        </span>
        <button
          type="button"
          className="btn-primary btn-inline"
          onClick={submit}
          disabled={!trimmed || posting}
        >
          {posting ? "Posting…" : "Post comment"}
        </button>
      </div>
    </div>
  );
}
