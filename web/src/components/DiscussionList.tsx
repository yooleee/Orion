// =============================================================================
// web/src/components/DiscussionList.tsx
// -----------------------------------------------------------------------------
// Responsible for: Rendering a project's two-way discussion thread (E2 Inc 5) —
//                  avatar + author + ROLE BADGE + relative time + body — or a clean
//                  empty state. The role badge (supervisor / developer) is what makes
//                  the back-and-forth legible at a glance.
// Role in project: The supervisor-interaction loop's read surface. Like a comment body,
//                  a discussion body is user-authored, attacker-influenceable text, so it
//                  is rendered as an inert React text node (never dangerouslySetInnerHTML).
//                  A guarantee-test (DiscussionList.test.tsx) pins that.
// =============================================================================

import type { DiscussionItem } from "../api/types";
import { relativeTime } from "../lib/time";

export function DiscussionList({ items }: { items: DiscussionItem[] }) {
  if (items.length === 0) {
    return <div className="comments-empty">No discussion yet.</div>;
  }
  return (
    <div className="comment-list">
      {items.map((d) => (
        <div className="comment" key={d.id}>
          <div className="avatar" aria-hidden="true">
            {d.author_name.charAt(0).toUpperCase()}
          </div>
          <div className="comment-body-wrap">
            <div className="comment-meta">
              <span className="comment-author">{d.author_name}</span>
              {/* A real role here (unlike comments): colour the pill by who is speaking. */}
              <span className={`discussion-role discussion-role--${d.role}`}>{d.role}</span>
              <span className="comment-time">{relativeTime(d.created_at)}</span>
            </div>
            {/* Stored, untrusted text — rendered as an inert React text node. */}
            <div className="comment-text">{d.body}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
