# =============================================================================
# collectors/about.py
# -----------------------------------------------------------------------------
# Responsible for: Extracting a project's "About" line — a short, plain-language
#                  statement of what the project IS — from its own configured doc
#                  (typically README.md), mechanically and with no LLM.
# Role in project: The producer half of the KB-surface "About band" (S2.1 Unit 2).
#                  A pure read_about(path) that the report/checklist-push paths call to
#                  resolve the About text a project rides to the relay, exactly as
#                  due_soon_days rides from config. Observe-not-originate: it reframes
#                  the doc's own first paragraph verbatim, it never authors or cleans up.
# Assumptions: The doc is UTF-8 Markdown. A missing/unreadable/empty doc yields None
#                  (absent), NOT "" — the caller renders no band rather than an empty one.
#                  This is a deliberately small, dependency-free skimmer for the one
#                  construct we want (the opening prose paragraph), not a Markdown parser.
# =============================================================================

from __future__ import annotations

from pathlib import Path

# The extraction cap, in characters. About is a one-glance line on a card/header, not a
# summary, so a long opening paragraph is truncated on a word boundary with an ellipsis.
# Applied at EXTRACTION (here) so the cap is enforced before the text ever rides the wire.
_ABOUT_CAP = 400


def _is_prose_line(line: str) -> bool:
    """Decide whether a single line is prose we should include in the About paragraph.

    Args:
        line: One already-stripped line of the doc (no surrounding whitespace).

    Returns:
        True if the line is ordinary prose to keep; False for structural/decorative
        lines a README opens with that are not "what this project is" text.

    Why:
        READMEs commonly open with a title, a row of shields.io badges, or an HTML
        banner before the first real sentence. We skip those mechanically so the first
        PROSE paragraph is what surfaces — without an LLM and without "cleaning up" the
        text itself (we only choose which lines count, we never rewrite them). The rules
        are intentionally coarse: anything not obviously structural is treated as prose.
    """
    if not line:
        # Blank line — handled by the caller as a paragraph boundary, never "prose".
        return False
    # ATX headings ("# Title", "## ..."): the title/section labels, not the description.
    if line.startswith("#"):
        return False
    # Thematic breaks / setext underlines ("---", "***", "===") — pure structure.
    if set(line) <= {"-", "*", "=", " "} and any(c in line for c in "-*="):
        return False
    # A raw HTML block (e.g. "<p align=\"center\">", "<img ...>", "<div>") — decorative
    # banners, not prose. We check the first char so an inline tag mid-sentence still
    # counts as prose; a line that STARTS with "<" is treated as markup.
    if line.startswith("<"):
        return False
    # A badge/image-only line: Markdown images "![alt](url)" or linked badges
    # "[![alt](img)](href)". These open with "![" or "[!". A line that merely CONTAINS
    # an inline image but starts with text is still prose (caught by the startswith).
    if line.startswith("![") or line.startswith("[!"):
        return False
    # A table row / separator ("| a | b |") — structured data, not a description.
    if line.startswith("|"):
        return False
    return True


def _cap(text: str) -> str:
    """Truncate About text to _ABOUT_CAP characters on a word boundary, with an ellipsis.

    Args:
        text: The extracted, whitespace-collapsed About paragraph.

    Returns:
        `text` unchanged when within the cap; otherwise a prefix cut at the last word
        boundary at or before the cap, with a trailing "…".

    Why:
        About is a single glance-able line, so an over-long opening paragraph is bounded
        here at extraction (before the wire) rather than trusting every render surface to
        clamp it. Cutting on a space avoids slicing a word in half; the ellipsis signals
        the text continues in the source doc.
    """
    if len(text) <= _ABOUT_CAP:
        return text
    clipped = text[:_ABOUT_CAP]
    # Prefer the last whole word; fall back to a hard cut if there is no space (a single
    # very long token), so we always return at most _ABOUT_CAP + 1 (the ellipsis) chars.
    boundary = clipped.rfind(" ")
    if boundary > 0:
        clipped = clipped[:boundary]
    return clipped.rstrip() + "…"


def read_about(path: Path) -> str | None:
    """Read a project's About line: the first prose paragraph of its configured doc.

    Args:
        path: Absolute path to the project's About doc (resolved from `about_file` in
            config, typically the repo's README.md).

    Returns:
        The first prose paragraph as a single capped line, or None when the doc is
        missing/unreadable/empty or contains no prose (only headings/badges/markup).
        None is deliberately distinct from "": absent means "render no About band",
        never "an empty band" — the same absent-vs-empty discipline the checklist uses.

    Why:
        The About band states what a project IS, observed from the project's own docs
        with no LLM (structured lane). The first paragraph after the badges/title is the
        conventional one-line description a README opens with, so skimming to it is a
        faithful, mechanical reframe (observe-not-originate). Reading fails soft to None
        so a mis-pointed or not-yet-written doc simply yields no band, never an error that
        blocks a report or push.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Missing, unreadable, or non-UTF-8: observe nothing rather than fail the push.
        return None

    # Walk lines, skipping any leading structural/decorative run (title, badges, HTML),
    # then accumulate the FIRST contiguous run of prose lines. A blank line AFTER prose
    # has begun ends the paragraph; blank/structural lines BEFORE it are just skipped.
    paragraph: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if _is_prose_line(line):
            paragraph.append(line)
        elif paragraph:
            # First non-prose line after prose started → the paragraph is complete.
            break
        # else: still in the leading structural run — keep skipping.

    if not paragraph:
        return None

    # Join wrapped lines with single spaces and collapse any internal whitespace runs, so
    # a hard-wrapped source paragraph reads as one clean line. split()/join collapses all
    # runs (spaces, tabs) in one step.
    collapsed = " ".join(" ".join(paragraph).split())
    if not collapsed:
        return None
    return _cap(collapsed)
