// =============================================================================
// web/src/components/CommentList.test.tsx
// -----------------------------------------------------------------------------
// GUARANTEE TEST — "untrusted text renders inert" (the comment path).
// Responsible for: Pinning that a comment BODY — the one piece of user-authored,
//                  attacker-influenceable content on the dashboard — renders as inert
//                  text, never as live DOM. Complements the Sidebar inert-text test
//                  (project names) by covering the highest-risk surface.
// =============================================================================

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CommentList } from "./CommentList";
import type { Comment } from "../api/types";

const EVIL_BODY = "<script>window.__pwned=1</script><img src=x onerror=window.__pwned=1>";

describe("CommentList — comment bodies render inert", () => {
  it("renders a malicious comment body as text, injecting no <script>/<img>", () => {
    const comments: Comment[] = [
      {
        id: 1,
        author: "Alex",
        role: null,
        body: EVIL_BODY,
        created_at: "2026-06-26T10:00:00+00:00",
      },
    ];
    const { container } = render(<CommentList comments={comments} />);

    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    // The raw markup is present verbatim as inert text.
    expect(container.textContent).toContain(EVIL_BODY);
  });

  it("shows a clean empty state when there are no comments", () => {
    const { getByText } = render(<CommentList comments={[]} />);
    expect(getByText("No comments yet.")).toBeInTheDocument();
  });
});
