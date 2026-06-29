// =============================================================================
// web/src/components/DiscussionComposer.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the composer's ROLE gating (only a supervisor or the developer
//                  may post; a viewer sees a read-only state) and the happy-path post (the
//                  typed body is sent for the project and the created item is handed back).
// Why these: the gating is the security-visible surface (a viewer must not get an active
//            composer, mirroring the server's 403), and the post wiring is the feature.
// =============================================================================

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { DiscussionComposer } from "./DiscussionComposer";
import { postDiscussion } from "../api/client";

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {},
  postDiscussion: vi.fn(),
}));
const mockPost = vi.mocked(postDiscussion);

beforeEach(() => mockPost.mockReset());

describe("DiscussionComposer — gating", () => {
  it("is read-only when the viewer cannot post (a viewer / not a participant)", () => {
    render(
      <DiscussionComposer projectName="demo" authorName="Kid" canDiscuss={false} onPosted={() => {}} />,
    );
    expect(screen.getByText("Read-only")).toBeInTheDocument();
    expect(
      screen.getByText("Only supervisors and the developer can post to the discussion."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /post message/i })).toBeDisabled();
  });

  it("shows 'Posting as {name}' when a supervisor/developer may post", () => {
    render(
      <DiscussionComposer projectName="demo" authorName="Dad" canDiscuss={true} onPosted={() => {}} />,
    );
    expect(screen.getByText("Posting as Dad")).toBeInTheDocument();
  });
});

describe("DiscussionComposer — posting", () => {
  it("posts the typed body for the project and hands the created item to onPosted", async () => {
    const created = {
      id: 9, author_name: "Dad", role: "supervisor" as const, body: "Nice work",
      created_at: "2026-06-28T00:00:00+00:00",
    };
    mockPost.mockResolvedValue(created);
    const onPosted = vi.fn();
    render(
      <DiscussionComposer projectName="demo" authorName="Dad" canDiscuss={true} onPosted={onPosted} />,
    );

    fireEvent.change(screen.getByPlaceholderText("Add to the discussion…"), {
      target: { value: "Nice work" },
    });
    fireEvent.click(screen.getByRole("button", { name: /post message/i }));

    await waitFor(() => expect(onPosted).toHaveBeenCalledWith(created));
    expect(mockPost).toHaveBeenCalledWith("demo", "Nice work"); // (projectName, trimmed body)
  });

  it("does not post an empty/whitespace body", () => {
    render(
      <DiscussionComposer projectName="demo" authorName="Dad" canDiscuss={true} onPosted={() => {}} />,
    );
    fireEvent.change(screen.getByPlaceholderText("Add to the discussion…"), {
      target: { value: "   " },
    });
    expect(screen.getByRole("button", { name: /post message/i })).toBeDisabled();
    expect(mockPost).not.toHaveBeenCalled();
  });
});
