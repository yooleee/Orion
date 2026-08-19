// =============================================================================
// web/src/lib/highlight.tsx
// -----------------------------------------------------------------------------
// Responsible for: highlightTerms — turning a plain-text search snippet + the query
//                  terms into React nodes with each match wrapped in <mark>.
// Role in project: The client half of the search contract's escape-before-highlight
//                  rule: the server ships snippets as RAW plain text, so highlighting
//                  must be built from split TEXT nodes (React escapes text children by
//                  construction), never by assembling HTML strings. A guarantee-test
//                  (highlight.test.tsx) pins that markup-shaped text renders inert.
// =============================================================================

import type { ReactNode } from "react";

/** Escape a user-typed term for literal use inside a RegExp (mirrors the server
 *  escaping LIKE wildcards: "100%" or "a.b" must match itself, not act as a pattern). */
function escapeRegExp(term: string): string {
  return term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Wrap every case-insensitive occurrence of any `term` in `text` with <mark>.
 *
 * Args:
 *   text: the plain-text snippet (or title) to render.
 *   terms: the whitespace-split query terms. Empty terms are dropped.
 *
 * Returns: an array of React nodes — plain strings for unmatched stretches, <mark>
 * elements for matches (original casing preserved). Just [text] when there is nothing
 * to highlight.
 *
 * Why: one alternation regex, longest term first, so a term that contains another
 * ("relays" vs "relay") highlights as the longer match instead of splitting it. The
 * split-with-capture-group idiom alternates unmatched/matched pieces, which maps
 * directly onto text-node children — the shape that makes the inert-markup guarantee
 * hold by construction.
 */
export function highlightTerms(text: string, terms: string[]): ReactNode[] {
  const cleaned = [...new Set(terms.filter((t) => t.length > 0))].sort(
    (a, b) => b.length - a.length,
  );
  if (cleaned.length === 0 || text === "") {
    return [text];
  }
  const pattern = new RegExp(`(${cleaned.map(escapeRegExp).join("|")})`, "gi");
  // With a capture group, split() keeps the matched pieces: even indices are the
  // unmatched stretches, odd indices the matches.
  return text.split(pattern).map((piece, i) =>
    i % 2 === 1 ? <mark key={i}>{piece}</mark> : piece,
  );
}
