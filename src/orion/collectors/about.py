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

import re
from pathlib import Path

# The extraction cap, in characters. About is a one-glance line on a card/header, not a
# summary, so a long opening paragraph is truncated. Applied at EXTRACTION (here) so the cap
# is enforced before the text ever rides the wire.
_ABOUT_CAP = 400

# When truncating, prefer ending on a COMPLETE SENTENCE rather than mid-clause — but only if
# doing so still keeps a useful amount of text. A paragraph opening with a very short first
# sentence ("Status: alpha.") would otherwise collapse the whole About to those two words, so
# below this floor we fall back to a word-boundary cut with an ellipsis.
_MIN_SENTENCE_CHARS = _ABOUT_CAP // 2

# A sentence terminator followed by whitespace or the end of the clipped text. Deliberately
# naive: an abbreviation ("e.g.", "Dr.") can read as a sentence end, which at worst cuts a
# little early. Since we take the LAST match within the cap, that error is bounded and quiet.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")

# --- Inline Markdown flattening ---------------------------------------------------------
# A README is a MARKDOWN file, but About renders as PLAIN TEXT on the dashboard, so emphasis
# syntax would otherwise show up as literal asterisks ("A **local-first** tool"). Stripping
# the markers is a MEDIUM TRANSLATION, not an edit: the author's word is preserved exactly,
# only the formatting notation that the destination cannot render is dropped. (The same
# reasoning the Slack mrkdwn translator already applies for its destination.)
#
# Deliberately CONSERVATIVE — single-underscore and single-asterisk italics are NOT handled,
# because `snake_case_names`, file globs and math would be mangled by them. Only the
# unambiguous constructs below are touched.
_INLINE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")  # ![alt](url) -> dropped entirely
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")  # [text](url) -> text
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(.+?)\*\*\*")  # ***text***  -> text
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")  # **text**    -> text
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")  # `code`      -> code


def _flatten_markdown(text: str) -> str:
    """Strip inline Markdown notation the plain-text destination cannot render.

    Args:
        text: The extracted paragraph, still carrying Markdown inline syntax.

    Returns:
        The same prose with emphasis/code markers removed, links reduced to their link
        TEXT, and inline images dropped. Word content is never altered.

    Why:
        About is observed from a Markdown doc and rendered as plain text, so leaving the
        notation in would surface literal `**` to a reader — a rendering artifact, not the
        author's meaning. Flattening keeps observe-not-originate intact (nothing is reworded
        or invented) while making the observation legible in the medium it lands in.
    """
    text = _INLINE_IMAGE_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _BOLD_ITALIC_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    # Dropping an inline image can leave a double space behind; re-collapse runs.
    return " ".join(text.split())


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
    """Truncate About text to _ABOUT_CAP characters, preferring a whole-sentence ending.

    Args:
        text: The extracted, whitespace-collapsed About paragraph.

    Returns:
        `text` unchanged when within the cap. Otherwise the longest prefix that ends on a
        SENTENCE boundary within the cap (returned as-is, with no ellipsis — it is a
        complete thought); or, when no sentence ends late enough to keep a useful amount of
        text, a prefix cut at the last word boundary with a trailing "…".

    Why:
        About is a single glance-able line, so an over-long opening paragraph is bounded
        here at extraction (before the wire) rather than trusting every render surface to
        clamp it. Cutting mid-clause reads as broken and can drop the qualifier that carried
        the meaning (Orion's own README loses "before anything is sent" that way, turning a
        privacy guarantee into a dangling phrase). Ending on the last complete sentence
        inside the cap keeps the text honest and readable; the word-boundary + ellipsis path
        remains for prose that has no sentence break to land on.
    """
    if len(text) <= _ABOUT_CAP:
        return text
    clipped = text[:_ABOUT_CAP]

    # Prefer the LAST sentence terminator inside the cap, provided it retains enough text to
    # still describe the project (see _MIN_SENTENCE_CHARS).
    sentence_ends = list(_SENTENCE_END_RE.finditer(clipped))
    if sentence_ends:
        end = sentence_ends[-1].end()
        if end >= _MIN_SENTENCE_CHARS:
            return clipped[:end].rstrip()

    # No usable sentence break: fall back to the last whole word (a hard cut only when the
    # text is a single very long token), so we return at most _ABOUT_CAP + 1 chars.
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
        faithful, mechanical reframe (observe-not-originate): the words are the author's,
        and only Markdown notation the plain-text destination cannot render is dropped.
        Reading fails soft to None so a mis-pointed or not-yet-written doc simply yields no
        band, never an error that blocks a report or push.
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
    # Then drop the inline Markdown notation the plain-text destination cannot render, and
    # only cap AFTER flattening — so the budget is spent on words, not on syntax that will
    # not be shown anyway.
    flattened = _flatten_markdown(collapsed)
    if not flattened:
        return None
    return _cap(flattened)
