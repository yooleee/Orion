// =============================================================================
// web/src/components/CommentComposer.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning the composer's gating (who can comment, and when there's
//                  no report to attach to) and the happy-path post (the typed body is
//                  sent and the created comment is handed back to the parent).
// Why these: the gating is the security-visible surface (a non-authed viewer must not
//            get an active composer), and the post wiring is the feature itself.
// =============================================================================

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { CommentComposer } from "./CommentComposer";
import { postComment } from "../api/client";

// Mock the client so the test never hits the network; ApiError is stubbed because the
// composer's catch branch does `instanceof ApiError`.
vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {},
  postComment: vi.fn(),
}));
const mockPost = vi.mocked(postComment);

beforeEach(() => mockPost.mockReset());

describe("CommentComposer — gating", () => {
  it("is disabled with 'Sign in to comment' when the viewer cannot comment", () => {
    render(<CommentComposer reportId={1} authorName={null} canComment={false} onPosted={() => {}} />);
    expect(screen.getByText("Sign in to comment")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /post comment/i })).toBeDisabled();
  });

  it("is disabled when there is no report to attach to", () => {
    render(<CommentComposer reportId={null} authorName="Yusuf" canComment={true} onPosted={() => {}} />);
    expect(screen.getByText("No reports yet to comment on")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /post comment/i })).toBeDisabled();
  });

  it("shows 'Commenting as {name}' when authed with a report to attach to", () => {
    render(<CommentComposer reportId={5} authorName="Yusuf" canComment={true} onPosted={() => {}} />);
    expect(screen.getByText("Commenting as Yusuf")).toBeInTheDocument();
  });
});

describe("CommentComposer — posting", () => {
  it("posts the typed body and hands the created comment to onPosted", async () => {
    const created = {
      id: 9, author: "Yusuf", role: null, body: "Nice work",
      created_at: "2026-06-27T00:00:00+00:00",
    };
    mockPost.mockResolvedValue(created);
    const onPosted = vi.fn();
    render(<CommentComposer reportId={5} authorName="Yusuf" canComment={true} onPosted={onPosted} />);

    fireEvent.change(screen.getByPlaceholderText("Add a comment…"), {
      target: { value: "Nice work" },
    });
    fireEvent.click(screen.getByRole("button", { name: /post comment/i }));

    await waitFor(() => expect(onPosted).toHaveBeenCalledWith(created));
    expect(mockPost).toHaveBeenCalledWith(5, "Nice work"); // (reportId, trimmed body)
  });

  it("does not post an empty/whitespace body", () => {
    render(<CommentComposer reportId={5} authorName="Yusuf" canComment={true} onPosted={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText("Add a comment…"), { target: { value: "   " } });
    // The button stays disabled for a whitespace-only body, so no post fires.
    expect(screen.getByRole("button", { name: /post comment/i })).toBeDisabled();
    expect(mockPost).not.toHaveBeenCalled();
  });
});
