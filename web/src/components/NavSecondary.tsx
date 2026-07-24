// =============================================================================
// web/src/components/NavSecondary.tsx
// -----------------------------------------------------------------------------
// Responsible for: The secondary-block pieces both primary navs show — the account card
//                  (avatar + name + role + logout) and the "Public showcase" link. The
//                  theme switcher is already its own shared component (ThemeSwitcher).
// Role in project: The desktop sidebar's bottom block and the mobile "More" sheet showed
//                  near-identical copies of these (DR1-R U2 removed that duplication). They
//                  live here as small pieces rather than one fixed "secondary block" because
//                  the two surfaces order them differently (sidebar: showcase, theme,
//                  account; sheet: account, showcase, theme), so each composes them itself.
// =============================================================================

import { Link } from "react-router-dom";
import type { Identity } from "../api/types";

/**
 * The signed-in account card: avatar initial, name, role, and a logout button.
 *
 * Args:
 *   identity: the signed-in account (name + role), or null on an open/anonymous relay.
 *   onLogout: called when the logout button is pressed.
 *   className: extra class appended to the base "account" class (the mobile sheet adds
 *     "more-account" for its own spacing).
 *
 * Returns: the account card, or null when there is no signed-in identity to show.
 *
 * Why: an anonymous relay has no account to render, so guarding on identity HERE means each
 *   caller just drops <AccountCard/> in without repeating the null check — the block was the
 *   largest duplicated piece across the two navs.
 */
export function AccountCard({
  identity,
  onLogout,
  className,
}: {
  identity: Identity | null;
  onLogout: () => void;
  className?: string;
}) {
  if (!identity) return null;
  return (
    <div className={`account${className ? ` ${className}` : ""}`}>
      <div className="avatar" aria-hidden="true">
        {identity.name.charAt(0).toUpperCase()}
      </div>
      <div>
        <div className="account-name">{identity.name}</div>
        <div className="account-role">{identity.role}</div>
      </div>
      <button
        type="button"
        className="account-logout"
        aria-label="Log out"
        title="Log out"
        onClick={onLogout}
      >
        ⏻
      </button>
    </div>
  );
}

/**
 * The "Public showcase" link (↗ + label) into the public Showcase surface.
 *
 * Args:
 *   onNavigate: optional callback fired on click — the mobile sheet uses it to close itself
 *     as it navigates; the desktop sidebar passes nothing.
 *
 * Returns: the showcase link.
 *
 * Why: the caller gates this on me.showcase_enabled (a relay capability, decided at the call
 *   site), so this component is just the shared markup — no capability logic of its own.
 */
export function ShowcaseLink({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <Link to="/showcase" className="showcase-link" onClick={onNavigate}>
      <span aria-hidden="true">↗</span>
      <span>Public showcase</span>
    </Link>
  );
}
