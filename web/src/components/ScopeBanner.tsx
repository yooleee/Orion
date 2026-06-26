// =============================================================================
// web/src/components/ScopeBanner.tsx
// -----------------------------------------------------------------------------
// Responsible for: The banner a scoped viewer sees on the home ("You're viewing
//                  shared work · N project(s) granted"), making the narrowed view
//                  feel intentional, not broken.
// Role in project: Shown only when the viewer's scope is restricted (a set of granted
//                  projects), never for an admin / open relay.
// =============================================================================

import type { Scope } from "../api/types";

export function ScopeBanner({ scope }: { scope: Scope }) {
  // Only meaningful for a restricted viewer; an unrestricted scope renders nothing.
  if (scope.unrestricted || scope.projects === null) return null;
  const n = scope.projects.length;
  return (
    <div className="scope-banner">
      ◆ You're viewing shared work · {n} project{n === 1 ? "" : "s"} granted
    </div>
  );
}
