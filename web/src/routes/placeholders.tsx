// =============================================================================
// web/src/routes/placeholders.tsx
// -----------------------------------------------------------------------------
// Responsible for: Small placeholder screens for routes whose real build is a later
//                  slice (Tracker page → next; the NEW sections → 4b/4c/4d) and a
//                  catch-all Not Found.
// Role in project: Keeps the routing table complete and navigable now, so the shell's
//                  nav + breadcrumbs work end to end before those screens exist.
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

export function Todos() {
  return (
    <Placeholder
      eyebrow="to-dos"
      title="To-dos"
      note="The trackers overview renders in a later slice."
    />
  );
}

export function NotFound() {
  return (
    <Placeholder eyebrow="not found" title="Not found" note="That page does not exist." />
  );
}
