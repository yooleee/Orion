# =============================================================================
# tests/test_about_collector.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying read_about — the mechanical, no-LLM extraction of a
#                  project's "About" line (first prose paragraph) from its doc.
# Role in project: The About band (KB-surface Unit 2) surfaces this text on every
#                  card/header. The extraction must be faithful (verbatim reframe),
#                  robust to the junk a README opens with (title, badges, HTML), and
#                  honest about absence (None, never "") so the caller can render no
#                  band rather than an empty one. These tests pin those edges.
# =============================================================================

from orion.collectors.about import _ABOUT_CAP, read_about


def _write(tmp_path, text, name="README.md"):
    """Write a doc file and return its path.

    Why: every test needs a throwaway doc on disk; this keeps each test to the one
    document it cares about instead of repeating file I/O (DRY).
    """
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_first_paragraph_after_title(tmp_path):
    """A normal README yields the first prose paragraph, not the title.

    Why this matters: this is the common case — the one-line description a README
    puts right under its "# Title" is exactly the "what this project is" text.
    """
    path = _write(tmp_path, "# Orion\n\nOrion turns project activity into progress updates.\n")
    assert read_about(path) == "Orion turns project activity into progress updates."


def test_skips_leading_badges_and_html(tmp_path):
    """Badge rows and an HTML banner before the prose are skipped.

    Why this matters: shields.io badge rows and "<p align=center>" banners are the
    most common opening junk; the first REAL sentence must win, mechanically.
    """
    path = _write(
        tmp_path,
        "# Project\n\n"
        '<p align="center">a logo</p>\n\n'
        "![build](https://img.shields.io/x) [![cov](img)](href)\n\n"
        "The actual description lives here.\n",
    )
    assert read_about(path) == "The actual description lives here."


def test_wrapped_paragraph_is_joined_to_one_line(tmp_path):
    """A hard-wrapped opening paragraph collapses to a single clean line.

    Why this matters: About renders as one glance-able line, so source line wraps
    must not leak through as ragged breaks or double spaces.
    """
    path = _write(
        tmp_path,
        "# X\n\nThis project does one thing\nand does it across\nseveral wrapped lines.\n",
    )
    assert read_about(path) == "This project does one thing and does it across several wrapped lines."


def test_stops_at_blank_line_after_prose(tmp_path):
    """Only the FIRST paragraph is taken; a following paragraph is excluded.

    Why this matters: About is a one-line intro, not the whole intro section — the
    blank line between paragraphs is the boundary.
    """
    path = _write(tmp_path, "# X\n\nFirst paragraph.\n\nSecond paragraph should be ignored.\n")
    assert read_about(path) == "First paragraph."


def test_missing_file_returns_none(tmp_path):
    """A configured-but-absent doc yields None (no band), never an error.

    Why this matters: about_file may point at a doc written later; read_about must
    fail soft so it never blocks a report or push. Absent is None, not "".
    """
    assert read_about(tmp_path / "does-not-exist.md") is None


def test_empty_file_returns_none(tmp_path):
    """An empty doc yields None — absent, not an empty band.

    Why this matters: absent vs empty is a real distinction (mirrors the checklist's
    None-vs-[] rule); the caller renders nothing for None.
    """
    assert read_about(_write(tmp_path, "")) is None


def test_only_headings_and_badges_returns_none(tmp_path):
    """A doc with no prose (only title + badges) yields None.

    Why this matters: there is genuinely no description to observe here; observing
    nothing beats surfacing a badge URL as if it were the project's purpose.
    """
    path = _write(tmp_path, "# Title\n\n![b](https://img.shields.io/x)\n\n## Section\n")
    assert read_about(path) is None


def test_long_paragraph_is_capped_on_word_boundary(tmp_path):
    """An over-long opening paragraph is truncated at the cap with an ellipsis.

    Why this matters: About is bounded at extraction (before the wire), and the cut
    lands on a space so no word is sliced in half.
    """
    # 60 five-char words ("wordX ") = ~360 chars, then a long tail to exceed the cap.
    body = " ".join(f"word{i:02d}" for i in range(80))  # ~560 chars, well over the cap
    path = _write(tmp_path, f"# X\n\n{body}\n")
    result = read_about(path)
    assert result is not None
    assert result.endswith("…")
    # At most the cap plus the single ellipsis char, and no trailing partial word/space.
    assert len(result) <= _ABOUT_CAP + 1
    assert not result[:-1].endswith(" ")


def test_prose_starting_before_any_heading(tmp_path):
    """A doc that opens directly with prose (no title) still yields it.

    Why this matters: not every doc leads with a "# Title"; the first prose line is
    the description wherever it sits.
    """
    path = _write(tmp_path, "Just a plain description with no heading at all.\n")
    assert read_about(path) == "Just a plain description with no heading at all."


# --- Inline Markdown flattening -------------------------------------------------------
# A README is Markdown but About renders as PLAIN TEXT, so notation the destination cannot
# render is stripped. The words themselves are never altered (observe-not-originate).


def test_flattens_bold_and_inline_code(tmp_path):
    """Bold and inline-code markers are dropped, keeping the author's words intact.

    Why this matters: without this the dashboard shows literal asterisks ("A **local-first**
    tool") — a rendering artifact, not what the author meant. Orion's own README hits this.
    """
    path = _write(tmp_path, "# X\n\nA **local-first** tool using `sqlite3` for state.\n")
    assert read_about(path) == "A local-first tool using sqlite3 for state."


def test_flattens_links_to_their_text_and_drops_inline_images(tmp_path):
    """A [text](url) link keeps its TEXT; an inline ![alt](url) image is dropped entirely.

    Why this matters: inline links are extremely common in a README's opening sentence, and
    the raw URL is noise in a one-line description. An inline image has no plain-text
    equivalent at all, so it is removed rather than rendered as bracket soup.
    """
    path = _write(
        tmp_path,
        "# X\n\nSee the [docs](https://example.com/docs) ![badge](https://img.io/b) for setup.\n",
    )
    assert read_about(path) == "See the docs for setup."


def test_does_not_touch_snake_case_or_single_asterisks(tmp_path):
    """Single `_`/`*` runs are deliberately LEFT ALONE — they mangle identifiers.

    Why this matters: this is the conservative boundary of the flattening. Treating a single
    underscore as italics would turn `snake_case_name` into `snakecasename`, silently
    corrupting an identifier the author wrote on purpose. Bounded flattening beats clever.
    """
    path = _write(tmp_path, "# X\n\nReads snake_case_name and a 3 * 4 grid.\n")
    assert read_about(path) == "Reads snake_case_name and a 3 * 4 grid."


# --- Sentence-aware capping -----------------------------------------------------------


def test_long_paragraph_ends_on_a_complete_sentence(tmp_path):
    """An over-long paragraph is cut at the last SENTENCE end inside the cap, no ellipsis.

    Why this matters: cutting mid-clause can drop the qualifier carrying the meaning — the
    real case is Orion's README, where a word-boundary cut turned "previewed in your terminal
    before anything is sent" into a dangling "previewed in your terminal…". A complete
    sentence reads as a finished thought, so no ellipsis is added.
    """
    first = "This project does the first thing well. " + ("filler words here " * 12)
    second = "And then a trailing sentence that pushes it over the cap entirely."
    path = _write(tmp_path, f"# X\n\n{first}{second}\n")
    result = read_about(path)
    assert result is not None
    assert result.endswith(".")  # a finished sentence
    assert not result.endswith("…")
    assert len(result) <= _ABOUT_CAP


def test_falls_back_to_word_boundary_when_the_first_sentence_is_tiny(tmp_path):
    """A very short opening sentence must NOT collapse the whole About to those few words.

    Why this matters: preferring a sentence end is only an improvement while it keeps enough
    text. "Alpha." followed by the real description would otherwise yield an About of one
    word, which is strictly worse than a truncated but informative line — so below the
    floor we fall back to the word-boundary cut with an ellipsis.
    """
    path = _write(tmp_path, "# X\n\nAlpha. " + ("descriptive filler text " * 30) + "\n")
    result = read_about(path)
    assert result is not None
    assert result != "Alpha."
    assert result.endswith("…")  # fell back to the word-boundary path
