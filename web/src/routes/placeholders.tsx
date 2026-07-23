// =============================================================================
// web/src/routes/placeholders.tsx
// -----------------------------------------------------------------------------
// Responsible for: The catch-all Not Found screen for unmatched routes.
// Role in project: Keeps the routing table complete — any path outside the real routes
//                  lands here rather than a blank shell.
// =============================================================================

function Placeholder({ eyebrow, title, note }: { eyebrow: string; title: string; note: string }) {
  return (
    <div>
      <div className="eyebrow">{eyebrow}</div>
      <h1 style={{ fontSize: 30, margin: "8px 0" }}>{title}</h1>
      <p style={{ color: "var(--tfaint)", fontFamily: "var(--font-mono)" }}>{note}</p>
    </div>
  );
}

export function NotFound() {
  return (
    <Placeholder eyebrow="not found" title="Not found" note="That page does not exist." />
  );
}
