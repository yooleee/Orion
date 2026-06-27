// =============================================================================
// web/src/components/ProgressBar.tsx
// -----------------------------------------------------------------------------
// Responsible for: The two progress-bar shapes the home uses — a plain done/total
//                  fill (project rows) and a segmented overdue/due-soon/remaining bar
//                  (the tracker card).
// Role in project: Pure presentation over the contract's progress/segments. Colours
//                  are CSS vars, so both bars re-theme for free.
// =============================================================================

import type { Progress, Segments } from "../api/types";

/**
 * A plain progress track with a single fill (accent, or --ok at 100%).
 *
 * Why: the project row's "X/Y · Z%" bar. The fill turns success-green only when
 * complete, matching the design's "done" treatment; otherwise it is the brand accent.
 */
export function ProgressBar({ progress }: { progress: Progress }) {
  const pct = progress.pct ?? 0;
  const complete = progress.total > 0 && progress.done >= progress.total;
  return (
    <div className="bar" aria-hidden="true">
      <div
        className="bar-fill"
        style={{ width: `${pct}%`, background: complete ? "var(--ok)" : "var(--accent)" }}
      />
    </div>
  );
}

/**
 * A segmented bar: overdue | due-soon | remaining-open | done widths over the total.
 *
 * Args:
 *   segments: the four bucket counts (they tile the total).
 *   total: the item total (the denominator for each segment's width).
 *
 * Why: the tracker card shows the shape of its workload, not just a single percentage.
 * Each segment's width is its share of the total; the four buckets sum to the total, so
 * the bar always fills. Empty segments render nothing (zero width).
 */
export function SegmentedBar({ segments, total }: { segments: Segments; total: number }) {
  // Order: done (settled) → remaining (neutral) → due-soon → overdue (most urgent last,
  // so the warm colours read together at the right). Each is a share of the total.
  const parts: Array<{ key: string; count: number; color: string }> = [
    { key: "done", count: segments.done, color: "var(--ok)" },
    { key: "remaining", count: segments.remaining, color: "var(--track)" },
    { key: "due_soon", count: segments.due_soon, color: "var(--due)" },
    { key: "overdue", count: segments.overdue, color: "var(--over)" },
  ];
  return (
    <div className="bar seg-bar" aria-hidden="true">
      {total > 0 &&
        parts.map((p) =>
          p.count > 0 ? (
            <div
              key={p.key}
              style={{ width: `${(p.count / total) * 100}%`, background: p.color }}
            />
          ) : null,
        )}
    </div>
  );
}
