// =============================================================================
// web/src/components/ScopeBanner.tsx
// -----------------------------------------------------------------------------
// Responsible for: The banner a scoped viewer sees on the home, making the narrowed
//                  view feel intentional rather than broken.
// Role in project: Shown only when the viewer's scope is restricted (a set of project
//                  names), never for an admin / open relay.
// Assumptions: `scope` comes from /api/portfolio; `role` from /api/me. A member's scope
//              arrives already resolved (org-visible ∪ grants), so this component only
//              chooses wording — it never computes access.
// =============================================================================

import type { Role, Scope } from "../api/types";

export function ScopeBanner({ scope, role }: { scope: Scope; role?: Role | null }) {
  // Only meaningful for a restricted viewer; an unrestricted scope renders nothing.
  if (scope.unrestricted || scope.projects === null) return null;
  const n = scope.projects.length;
  const plural = n === 1 ? "" : "s";
  // A MEMBER's projects are not "granted" — they are the org's shared work, visible
  // without any per-project grant (auth revamp, Unit 5). Saying "granted" to a member
  // with zero grants would describe their access wrongly, so the two cases get the
  // wording that is actually true of each rather than one phrase that fits neither.
  const text =
    role === "member"
      ? `You're viewing your organization's work · ${n} project${plural} visible`
      : `You're viewing shared work · ${n} project${plural} granted`;
  return <div className="scope-banner">◆ {text}</div>;
}
