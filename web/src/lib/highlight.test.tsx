// =============================================================================
// web/src/lib/highlight.test.tsx
// -----------------------------------------------------------------------------
// Responsible for: Pinning highlightTerms — the search screen's match highlighter —
//                  and above all the escape-before-highlight guarantee: markup-shaped
//                  snippet text renders INERT, highlighted or not.
// Role in project: Snippets arrive from the relay as raw plain text (the contract says
//                  the server does NOT HTML-escape them). If highlighting were ever
//                  rebuilt via HTML strings, a report body containing markup would
//                  execute in every reader's browser. The <script> pin below is the
//                  guard against that rebuild.
// =============================================================================

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { highlightTerms } from "./highlight";

/** Render the helper's output inside a div and return the container element. */
function renderHighlight(text: string, terms: string[]) {
  return render(<div>{highlightTerms(text, terms)}</div>).container;
}

describe("highlightTerms", () => {
  it("wraps each case-insensitive match in <mark> and leaves the rest as text", () => {
    // The core behavior: the term the user typed lights up regardless of the casing
    // the report used, and surrounding text is untouched.
    const el = renderHighlight("Deployed the Relay to Fly.", ["relay"]);
    const marks = el.querySelectorAll("mark");
    expect(marks).toHaveLength(1);
    expect(marks[0].textContent).toBe("Relay"); // original casing preserved
    expect(el.textContent).toBe("Deployed the Relay to Fly.");
  });

  it("highlights every term of a multi-term query, including repeats", () => {
    // AND-matched hits contain every term; each occurrence lights up so the reader
    // sees WHY the row matched, not just the first reason.
    const el = renderHighlight("auth work: the auth revamp", ["auth", "revamp"]);
    const marked = [...el.querySelectorAll("mark")].map((m) => m.textContent);
    expect(marked).toEqual(["auth", "auth", "revamp"]);
  });

  it("renders markup-shaped text inert — the escape-before-highlight pin", () => {
    // The load-bearing test. A stored body may legitimately contain angle brackets
    // (code snippets in reports); they must land as TEXT, never as elements. This
    // fails if highlightTerms is ever rebuilt via HTML strings instead of text nodes.
    const nasty = "beware <script>alert(1)</script> in bodies";
    const el = renderHighlight(nasty, ["script"]);
    expect(el.querySelector("script")).toBeNull(); // no element was created
    expect(el.textContent).toBe(nasty); // the text survives verbatim
    expect(el.querySelectorAll("mark").length).toBeGreaterThan(0); // and still highlights
  });

  it("returns the text unchanged when no term matches", () => {
    // A snippet can legitimately not contain a term (e.g. the match was in another
    // part of the body than the snippet window shows) — plain text, no marks.
    const el = renderHighlight("nothing to see here", ["absent"]);
    expect(el.querySelectorAll("mark")).toHaveLength(0);
    expect(el.textContent).toBe("nothing to see here");
  });

  it("treats regex metacharacters in a term as literals", () => {
    // Terms come straight from user input; "100%" or "a.b" must match literally, not
    // as a pattern (mirrors the server's LIKE-wildcard escaping, client-side).
    const el = renderHighlight("progress: 100% (a.b done)", ["100%", "a.b"]);
    const marked = [...el.querySelectorAll("mark")].map((m) => m.textContent);
    expect(marked).toEqual(["100%", "a.b"]);
    // ".": a literal dot must NOT match arbitrary characters elsewhere.
    const el2 = renderHighlight("axb is not a.b", ["a.b"]);
    expect([...el2.querySelectorAll("mark")].map((m) => m.textContent)).toEqual(["a.b"]);
  });
});
