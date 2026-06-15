# =============================================================================
# merge.py
# -----------------------------------------------------------------------------
# Responsible for: Combining the finished per-collector bodies of one run into a
#                  single report body, as titled Markdown sections.
# Role in project: The step BETWEEN collecting and building the report. The
#                  orchestrator collects (and, for the raw lane, summarizes) each
#                  enabled signal independently, then calls this to assemble one
#                  body that goes through the final redaction pass, compose, and
#                  delivery as a single message.
# Why a separate module (not in cli.py or compose.py): this is a small, pure,
#                  easily-tested function with no dependencies. Burying it in the
#                  orchestrator would make it untestable without the whole
#                  pipeline; putting it in compose.py would conflate "merge bodies"
#                  (lane-aware assembly) with "format a ReportBlob for a channel"
#                  (the last formatting step). Keeping it standalone preserves the
#                  single-responsibility seam the codebase favors.
# =============================================================================

from __future__ import annotations


def merge_sections(sections: list[tuple[str, str]]) -> str:
    """Combine titled per-collector bodies into one Markdown report body.

    Args:
        sections: Ordered (title, body) pairs — one per collector that produced
            output this run, in the order the report should present them. A body
            that is empty or whitespace-only is skipped (its collector had no
            activity, or contributed nothing after upstream processing).

    Returns:
        The merged body: each surviving section rendered as a "## {title}" header
        followed by its (stripped) body, sections separated by a blank line.
        Returns "" when every section was empty — the caller treats that as
        "nothing to report," exactly like a collector with no activity.

    Why:
        A single, sectioned body means one preview and one delivered message per
        run no matter how many signals fired, which is what a supervisor wants
        (one update, not a burst of pings). Headers are kept even for a single
        section: a consistent, labeled structure is more predictable for both the
        reader and the tests than conditionally dropping the heading, and the cost
        is one short line. This function only assembles already-finished text — it
        NEVER sends anything to the LLM, so structured sections stay structured.
    """
    parts: list[str] = []
    for title, body in sections:
        # Skip a section with no real content so we never emit a bare header.
        if not body or not body.strip():
            continue
        # strip() the body so section spacing is uniform regardless of stray
        # leading/trailing newlines a collector may have produced.
        parts.append(f"## {title}\n{body.strip()}")

    # A blank line between sections renders as a clear visual break in Markdown.
    return "\n\n".join(parts)
