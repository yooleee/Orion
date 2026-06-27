// =============================================================================
// web/src/components/StatusSignal.tsx
// -----------------------------------------------------------------------------
// Responsible for: Rendering one status as glyph + label + colour — the atom every
//                  forward-signal chip / indicator is built from.
// Role in project: Enforces the "state legible without colour alone" invariant BY
//                  CONSTRUCTION: it always renders the glyph AND a text label, never
//                  colour alone. Colour comes from the state's --token (status.ts), so
//                  it re-themes for free. A guarantee-test pins the glyph+label property.
// =============================================================================

import type { ReactNode } from "react";
import type { Status } from "../api/types";
import { STATUS_STYLES } from "../theme/status";

interface StatusSignalProps {
  state: Status;
  /** Optional override for the text — e.g. "5d overdue" instead of the default "overdue".
   *  Whatever is shown is still TEXT (legible without colour), preserving the invariant. */
  label?: ReactNode;
  /** When true, hide the glyph (rare — e.g. a neutral "upcoming" relative time). */
  hideGlyph?: boolean;
}

/**
 * Render a status as a coloured glyph + label.
 *
 * Args:
 *   state: the semantic status (the wire enum) → looked up in STATUS_STYLES.
 *   label: optional text to show instead of the default label.
 *   hideGlyph: drop the glyph (for the neutral "upcoming" case, which has none).
 *
 * Why: one component owns the glyph+label+colour rendering, so the accessibility
 * invariant cannot be forgotten at a call site, and a theme change only swaps the CSS
 * var. The colour is applied inline from the token name because it is data-driven (per
 * state), which a static class cannot express without a class-per-state explosion.
 */
export function StatusSignal({ state, label, hideGlyph }: StatusSignalProps) {
  const style = STATUS_STYLES[state];
  const text = label ?? style.label;
  return (
    <span className="status-signal" style={{ color: `var(${style.colorVar})` }}>
      {!hideGlyph && style.glyph ? <span aria-hidden="true">{style.glyph}</span> : null}
      <span>{text}</span>
    </span>
  );
}
