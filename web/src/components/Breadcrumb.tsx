// =============================================================================
// web/src/components/Breadcrumb.tsx
// -----------------------------------------------------------------------------
// Responsible for: The "projects / orion / report #26" breadcrumb above a detail
//                  page header. Each crumb except the last is a back-link.
// Role in project: Navigation affordance shared by the project + report pages.
// =============================================================================

import { Link } from "react-router-dom";

export interface Crumb {
  label: string;
  /** A route to link to; omit for the current (last) crumb, which renders as plain text. */
  to?: string;
}

export function Breadcrumb({ items }: { items: Crumb[] }) {
  return (
    <nav className="breadcrumb" aria-label="Breadcrumb">
      {items.map((c, i) => (
        <span key={i}>
          {i > 0 && <span className="crumb-sep"> / </span>}
          {c.to ? (
            <Link to={c.to} className="crumb-link">
              {c.label}
            </Link>
          ) : (
            <span className="crumb-current">{c.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}
