// =============================================================================
// web/src/components/DiscussionComposer.tsx
// -----------------------------------------------------------------------------
// Responsible for: The discussion composer — a controlled textarea + post button that
//                  appends to a project's supervisor↔developer thread via the cookie-authed
//                  JSON endpoint, handing the created item back to the parent to append.
// Role in project: The write surface of the supervisor-interaction loop (E2 Inc 5). Unlike
//                  the comment composer, it gates on ROLE: only a supervisor or the
//                  developer (admin) may post — a viewer sees a read-only state (the server
//                  enforces the same with a 403; this just avoids offering a dead control).
// Security: the body is sent as plain JSON and rendered back as an inert React text node
//           (DiscussionList). The server derives author/role from the session identity;
//           a typed-in name cannot spoof it, and a viewer cannot post.
// =============================================================================

import { useState } from "react";
import type { DiscussionItem } from "../api/types";
import { ApiError, postDiscussion } from "../api/client";

interface DiscussionComposerProps {
  /** The project whose thread to post to. */
  projectName: string;
  /** The signed-in participant's name, or null when not authenticated. */
  authorName: string | null;
  /** Whether this viewer may post: authenticated AND a supervisor or the developer. */
  canDiscuss: boolean;
  /** Called with the created item on a successful post, so the parent appends it. */
  onPosted: (item: DiscussionItem) => void;
}

export function DiscussionComposer({
  projectName,
  authorName,
  canDiscuss,
  onPosted,
}: DiscussionComposerProps) {
  const [body, setBody] = useState("");
  const [posting, setPosting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A clear, non-interactive state when posting isn't possible: a viewer (or anyone not
  // signed in) can read the thread but not join it — only supervisors and the developer do.
  if (!canDiscuss) {
    return (
      <div className="composer">
        <div className="composer-as">Read-only</div>
        <textarea className="composer-input" placeholder="Add to the discussion…" rows={3} disabled />
        <div className="composer-actions">
          <span className="composer-note">
            Only supervisors and the developer can post to the discussion.
          </span>
          <button type="button" className="btn-primary btn-inline" disabled>
            Post message
          </button>
        </div>
      </div>
    );
  }

  const trimmed = body.trim();

  async function submit() {
    if (!trimmed || posting) return;
    setPosting(true);
    setError(null);
    try {
      const created = await postDiscussion(projectName, trimmed);
      onPosted(created);
      setBody("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not post your message.");
    } finally {
      setPosting(false);
    }
  }

  return (
    <div className="composer">
      <div className="composer-as">
        {authorName ? `Posting as ${authorName}` : "Posting to the discussion"}
      </div>
      <textarea
        className="composer-input"
        placeholder="Add to the discussion…"
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
          {posting ? "Posting…" : "Post message"}
        </button>
      </div>
    </div>
  );
}
