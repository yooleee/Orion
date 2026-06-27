// =============================================================================
// web/src/theme/status.ts
// -----------------------------------------------------------------------------
// Responsible for: The single mapping from a semantic status (the wire enum) to its
//                  PRESENTATION — glyph + human label + colour token. This is the
//                  client-side half of "server-side derivation, presentation-side
//                  vocabulary": the relay decides WHICH state an item is in; this
//                  table decides how it LOOKS.
// Role in project: Every status chip/indicator renders through this table, so the
//                  "state legible without colour alone" invariant (glyph + label +
//                  colour, never colour alone) holds by construction — and a theme
//                  switch only changes the colour (a CSS var), never the glyph/label.
// Source: design/README.md "Status vocabulary (glyph · meaning · token)".
// =============================================================================

import type { Status } from "../api/types";

export interface StatusStyle {
  /** The Unicode glyph (design vocabulary). Empty string for the neutral "upcoming". */
  glyph: string;
  /** The human-readable label — the text half that keeps state legible without colour. */
  label: string;
  /** The CSS custom property (a --token name) carrying this state's foreground colour. */
  colorVar: string;
}

// The full status vocabulary. Keyed by the wire enum so a serializer state maps 1:1.
export const STATUS_STYLES: Record<Status, StatusStyle> = {
  not_started: { glyph: "○", label: "not started", colorVar: "--todo" },
  in_progress: { glyph: "◐", label: "in progress", colorVar: "--due" },
  done: { glyph: "✓", label: "done", colorVar: "--ok" },
  due_soon: { glyph: "◷", label: "due soon", colorVar: "--due" },
  overdue: { glyph: "▲", label: "overdue", colorVar: "--over" },
  // "upcoming" is a dated-but-not-soon deadline: no flag glyph, neutral colour. The chip
  // renders a relative time, not a glyph — so its glyph is empty and the colour is faint.
  upcoming: { glyph: "", label: "upcoming", colorVar: "--tlow" },
  at_risk: { glyph: "△", label: "at risk", colorVar: "--over" },
  slipping: { glyph: "↝", label: "slipping", colorVar: "--slip" },
  on_track: { glyph: "✓", label: "on track", colorVar: "--ok" },
};

// Source-tag glyphs (where a deadline/item comes from): a project vs. a tracker.
export const SOURCE_GLYPH = { project: "◇", tracker: "⊟" } as const;
