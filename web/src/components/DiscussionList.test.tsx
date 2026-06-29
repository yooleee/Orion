// =============================================================================
// web/src/components/DiscussionList.test.tsx
// -----------------------------------------------------------------------------
// GUARANTEE TEST — "untrusted text renders inert" (the discussion path), plus the
// role-badge rendering that makes the two-way thread legible.
// Responsible for: Pinning that a discussion BODY renders as inert text (never live DOM),
//                  the same XSS guarantee the comment path holds, and that each item shows
//                  its server-derived role badge.
// =============================================================================

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiscussionList } from "./DiscussionList";
import type { DiscussionItem } from "../api/types";

const EVIL_BODY = "<script>window.__pwned=1</script><img src=x onerror=window.__pwned=1>";

describe("DiscussionList — bodies render inert", () => {
  it("renders a malicious discussion body as text, injecting no <script>/<img>", () => {
    const items: DiscussionItem[] = [
      {
        id: 1,
        author_name: "Dad",
        role: "supervisor",
        body: EVIL_BODY,
        created_at: "2026-06-28T10:00:00+00:00",
      },
    ];
    const { container } = render(<DiscussionList items={items} />);

    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    // The raw markup is present verbatim as inert text.
    expect(container.textContent).toContain(EVIL_BODY);
  });

  it("shows each item's role badge so supervisor and developer turns are distinguishable", () => {
    const items: DiscussionItem[] = [
      { id: 1, author_name: "Dad", role: "supervisor", body: "How's auth?",
        created_at: "2026-06-28T10:00:00+00:00" },
      { id: 2, author_name: "Yusuf", role: "developer", body: "Landed.",
        created_at: "2026-06-28T11:00:00+00:00" },
    ];
    const { getByText, container } = render(<DiscussionList items={items} />);
    expect(getByText("supervisor")).toBeInTheDocument();
    expect(getByText("developer")).toBeInTheDocument();
    // The role drives the badge variant class (which the CSS colour-codes).
    expect(container.querySelector(".discussion-role--supervisor")).not.toBeNull();
    expect(container.querySelector(".discussion-role--developer")).not.toBeNull();
  });

  it("shows a clean empty state when there is no discussion yet", () => {
    const { getByText } = render(<DiscussionList items={[]} />);
    expect(getByText("No discussion yet.")).toBeInTheDocument();
  });
});
