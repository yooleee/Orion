// =============================================================================
// web/src/components/StatusSignal.test.tsx
// -----------------------------------------------------------------------------
// GUARANTEE TEST #1 — "state legible without colour alone".
// Responsible for: Pinning that every status renders a GLYPH and a TEXT LABEL, not
//                  colour alone. This is a hard accessibility + product invariant
//                  (design/README), so it is enforced by a test rather than left to
//                  per-call-site discipline.
// =============================================================================

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusSignal } from "./StatusSignal";
import { STATUS_STYLES } from "../theme/status";
import type { Status } from "../api/types";

describe("StatusSignal — legible without colour alone", () => {
  // Every flagged state must carry BOTH its glyph and a readable text label.
  const flagged: Status[] = [
    "not_started",
    "in_progress",
    "done",
    "due_soon",
    "overdue",
    "at_risk",
    "slipping",
    "on_track",
  ];

  it.each(flagged)("renders glyph + text label for state '%s'", (state) => {
    render(<StatusSignal state={state} />);
    const { glyph, label } = STATUS_STYLES[state];
    // The text label is present (the colour-independent meaning)...
    expect(screen.getByText(label)).toBeInTheDocument();
    // ...and so is the glyph (the second non-colour cue).
    expect(screen.getByText(glyph)).toBeInTheDocument();
  });

  it("still shows the text label for the neutral 'upcoming' (which has no glyph)", () => {
    // upcoming is the one state with no flag glyph; the label must still carry meaning.
    render(<StatusSignal state="upcoming" label="in 12d" />);
    expect(screen.getByText("in 12d")).toBeInTheDocument();
  });

  it("applies the state's colour as an ADDITION to the glyph+label, not instead", () => {
    render(<StatusSignal state="overdue" />);
    const el = screen.getByText("overdue").parentElement;
    // Colour is set (themeable), but the assertions above prove meaning survives without it.
    expect(el).toHaveStyle({ color: "var(--over)" });
  });
});
