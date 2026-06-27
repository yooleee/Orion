// =============================================================================
// web/src/components/MobileTopBar.tsx
// -----------------------------------------------------------------------------
// Responsible for: The mobile top app bar — the brand lockup (a home affordance) and,
//                  when signed in, an identity avatar. Sticks to the top of the viewport
//                  on narrow screens; hidden on desktop (the sidebar carries the brand).
// Role in project: The top half of the mobile shell chrome. Kept deliberately minimal:
//                  per-page back navigation already lives in the in-content Breadcrumb,
//                  so duplicating a breadcrumb here would be redundant — this bar only
//                  adds the persistent brand + identity the hidden sidebar used to show.
// =============================================================================

import { Link } from "react-router-dom";
import type { Me } from "../api/types";

export function MobileTopBar({ me }: { me: Me }) {
  return (
    <header className="mobile-topbar">
      <Link to="/" className="brand mobile-brand">
        <span className="brand-dot" aria-hidden="true" />
        <span>Orion</span>
      </Link>
      {me.identity && (
        <div className="avatar avatar-sm" aria-hidden="true">
          {me.identity.name.charAt(0).toUpperCase()}
        </div>
      )}
    </header>
  );
}
