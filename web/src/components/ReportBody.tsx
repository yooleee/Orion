// =============================================================================
// web/src/components/ReportBody.tsx
// -----------------------------------------------------------------------------
// Responsible for: The report's body card — the title and the labelled sections
//                  (SHIPPED / DIRECTION / NOTES …) with their prose underneath.
// Role in project: The left column of the report reader. Section bodies are stored,
//                  untrusted text rendered as inert React text (white-space preserved
//                  so any markers in the source show), never injected as HTML.
// =============================================================================

import type { ReportDetail } from "../api/types";

export function ReportBody({ report }: { report: ReportDetail }) {
  // Prefer the parsed sections (the structured content). Fall back to the raw body when a
  // report has no sections, so nothing is ever blank.
  const hasSections = report.sections.length > 0;
  return (
    <div className="report-card">
      <h2 className="report-title">{report.title || `Report #${report.id}`}</h2>
      {hasSections ? (
        report.sections.map(([label, body], i) => (
          <div className="report-section" key={i}>
            {label && <div className="eyebrow report-section-label">{label}</div>}
            <div className="report-prose">{body}</div>
          </div>
        ))
      ) : (
        <div className="report-prose">{report.body}</div>
      )}
    </div>
  );
}
