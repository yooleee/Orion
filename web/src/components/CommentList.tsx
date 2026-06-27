// =============================================================================
// web/src/components/CommentList.tsx
// -----------------------------------------------------------------------------
// Responsible for: Rendering a comment thread — avatar + author + relative time +
//                  body — or a clean empty state. Shared by the project + report pages.
// Role in project: Comments are the ONE kind of user-authored content, and a comment
//                  body is the most attacker-relevant stored text on the dashboard, so
//                  it is rendered as inert React text (never dangerouslySetInnerHTML).
//                  A guarantee-test (CommentList.test.tsx) pins that.
// =============================================================================

import type { Comment } from "../api/types";
import { relativeTime } from "../lib/time";

export function CommentList({ comments }: { comments: Comment[] }) {
  if (comments.length === 0) {
    return <div className="comments-empty">No comments yet.</div>;
  }
  return (
    <div className="comment-list">
      {comments.map((c) => {
        const name = c.author || "Anonymous";
        return (
          <div className="comment" key={c.id}>
            <div className="avatar" aria-hidden="true">
              {name.charAt(0).toUpperCase()}
            </div>
            <div className="comment-body-wrap">
              <div className="comment-meta">
                <span className="comment-author">{name}</span>
                {c.role && <span className="comment-role">{c.role}</span>}
                <span className="comment-time">{relativeTime(c.created_at)}</span>
              </div>
              {/* Stored, untrusted text — rendered as an inert React text node. */}
              <div className="comment-text">{c.body}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
