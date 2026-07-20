// =============================================================================
// web/src/components/AgentBadge.tsx
// -----------------------------------------------------------------------------
// Responsible for: The "agent" chip + "operated by <human>" line shown beside a
//                  report's producer name when that producer is an AGENT account
//                  (auth revamp, Unit 4a).
// Role in project: Used by both the project page's report timeline and the report
//                  detail page, so an agent's work is badged identically wherever it
//                  appears. Keeping it in one component is what guarantees that.
// Assumptions: `kind` and `operatedBy` come from the relay's read-time attribution
//              join (author_kind / operated_by_name on the report wire). Both are
//              null for a legacy push or a deleted account.
// =============================================================================

import type { AuthorKind } from "../api/types";

/**
 * Render the agent chip (and its operator) for a report, or nothing at all.
 *
 * Args:
 *   kind: The producer's account kind — "agent", "human", or null (unattributed).
 *   operatedBy: For an agent, the human account it acted on behalf of; else null.
 *
 * Returns:
 *   The chip + "operated by" text for an agent; null for every other case, so a
 *   human's report and a legacy report render exactly as they did before Unit 4a.
 *
 * Why:
 *   Agents act on their operator's behalf, so showing the agent alone would hide who
 *   is actually accountable — and showing only the human would erase that a machine
 *   did the work. The chip keeps BOTH facts visible, which is the honest-attribution
 *   property the whole agent model rests on.
 *
 *   Only "agent" renders. Badging "human" too would add noise to the overwhelmingly
 *   common case, and badging null would assert an identity the relay never recorded.
 */
export function AgentBadge({
  kind,
  operatedBy,
}: {
  kind: AuthorKind;
  operatedBy: string | null;
}) {
  if (kind !== "agent") return null;
  return (
    <>
      {" "}
      <span className="agent-chip">agent</span>
      {/* The operator can be absent if the operating account was deleted — the agent
          is still legitimately badged, we just have no human left to name. */}
      {operatedBy && <> · operated by {operatedBy}</>}
    </>
  );
}
