# =============================================================================
# tests/test_relay_render.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the dashboard's HTML render functions — that views
#                  contain the expected content, that empty states render, and —
#                  critically — that EVERY dynamic value is HTML-escaped (no XSS).
# Role in project: render.py is the relay's presentation layer and its security
#                  boundary against stored content becoming markup. These tests are
#                  pure (no server, no DB): they feed crafted dicts straight to the
#                  render functions, which is exactly how the XSS guarantee is
#                  pinned at its source.
# =============================================================================

from relay.render import (
    _format_ts,
    render_index,
    render_not_found,
    render_project,
    render_report,
)

# A payload that WOULD execute if rendered raw. Every test that injects it asserts
# it comes back escaped (as &lt;script&gt;...) and never as a live tag.
_XSS = "<script>alert('x')</script>"


def _report(**overrides):
    """A full report dict (get()'s shape) with sensible defaults, overridable.

    Why:
        render_report reads nine fields; defaulting them keeps each test to the one
        field it is exercising (e.g. inject _XSS into just the body) instead of
        restating the whole dict (DRY).
    """
    report = {
        "id": 1,
        "project": "demo",
        "body": "Shipped the seam.",
        "sections": [["Code activity", "Did X."]],
        "participants": ["Alex", "Sam"],
        "share_level": "high_level",
        "lane": "raw",
        "generated_at": "2026-06-18T00:00:00+00:00",
        "orion_version": "0.0.0",
        "ingested_at": "2026-06-18T00:00:01+00:00",
    }
    report.update(overrides)
    return report


# --- content / structure ------------------------------------------------------


def test_index_lists_projects_with_links():
    """The index shows each project name and links to its project page.

    Why this matters: the home view's whole job is to be the jump-off point —
    the project name must appear and its link must point at /project/<name>.
    """
    html = render_index(
        [{"project": "demo", "report_count": 3, "latest_generated_at": "2026-06-18T00:00:00+00:00"}]
    )
    assert "demo" in html
    assert 'href="/project/demo"' in html
    assert "3 report(s)" in html


def test_project_lists_reports_linking_to_each_report():
    """A project page lists its reports, each linking to /report/<id>.

    Why this matters: this is the timeline view; each entry must be clickable
    through to the full report by its id.
    """
    html = render_project("demo", [_report(id=7, generated_at="2026-06-18T09:00:00+00:00")])
    assert 'href="/report/7"' in html
    # The link text is the humanized timestamp (see test_project_humanizes_timestamp),
    # so the raw ISO string is no longer shown verbatim here.


def test_report_shows_sections_and_metadata():
    """A report page renders its section titles/bodies and key metadata.

    Why this matters: the leaf view is what a supervisor actually reads — the
    section content and the provenance metadata (recipients, lane) must be present.
    """
    html = render_report(
        _report(sections=[["Code activity", "Shipped the relay."], ["Notes", "Next: dashboard."]])
    )
    assert "Code activity" in html
    assert "Shipped the relay." in html
    assert "Notes" in html
    assert "Alex, Sam" in html  # participants joined
    assert "raw" in html         # lane


def test_report_without_sections_renders_the_flat_body():
    """A report with no sections (an intake push) renders its flat body.

    Why this matters: intake pushes carry a single body and an empty sections list;
    the view must still show the update (mirroring how the chat composer renders
    "no sections" as the body), not a blank page.
    """
    html = render_report(_report(sections=[], body="A pushed update."))
    assert "A pushed update." in html


# --- empty states -------------------------------------------------------------


def test_index_empty_state():
    """An index with no projects renders a friendly empty-state, not a bare page.

    Why this matters: a fresh relay (nothing pushed yet) should explain itself
    rather than look broken.
    """
    html = render_index([])
    assert "No reports yet" in html


def test_project_empty_state():
    """A project with no reports renders an empty-state (still a 200-style page).

    Why this matters: an unknown or never-pushed project is a normal case, shown as
    a clean empty page rather than an error.
    """
    html = render_project("ghost", [])
    assert "ghost" in html
    assert "No reports for this project yet" in html


# --- XSS: every dynamic value must be escaped ---------------------------------


def test_report_body_is_escaped():
    """A <script> in the report body is rendered escaped, never as a live tag.

    Why this matters: stored content is redacted but still arbitrary text — a commit
    message can contain HTML. Escaping is the guarantee that such content is shown
    as inert text and can never execute in a viewer's browser.
    """
    html = render_report(_report(sections=[], body=_XSS))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_report_section_title_and_body_are_escaped():
    """A <script> in a section title OR body is escaped.

    Why this matters: sections are dynamic on both axes; both the title and the body
    must be escaped, not just one.
    """
    html = render_report(_report(sections=[[_XSS, _XSS]]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_report_participant_name_is_escaped():
    """A <script> in a participant name is escaped.

    Why this matters: participant names come from config and flow into the metadata
    line; they are dynamic and must be escaped like everything else.
    """
    html = render_report(_report(participants=[_XSS]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_index_project_name_is_escaped():
    """A malicious project name is escaped in the index (text AND its href).

    Why this matters: the project name appears both as link text and inside the
    href; both must be safe. The percent-encoded href must not contain a raw "<",
    and the visible text must be escaped.
    """
    html = render_index(
        [{"project": _XSS, "report_count": 1, "latest_generated_at": "2026-06-18T00:00:00+00:00"}]
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_not_found_message_is_escaped():
    """The 404 message (which echoes the requested path) is escaped.

    Why this matters: the not-found message typically contains attacker-controllable
    input (the bad path/id), so it is a real XSS vector if rendered raw.
    """
    html = render_not_found(f"Unknown path {_XSS!r}.")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --- CP3 hardening: report id, breadcrumbs, timestamps, badges, guards ---------


def test_report_shows_its_id():
    """The report detail page shows "Report #<id>" in the metadata line.

    Why this matters: the URL carries /report/<id> but the page never echoed it;
    surfacing the id lets a reader cite or cross-reference a specific report.
    """
    html = render_report(_report(id=42))
    assert "Report #42" in html


def test_report_has_breadcrumb_back_to_project():
    """The report page links back to its project's history (← <project>).

    Why this matters: the leaf view was a dead end; a breadcrumb lets a reader step
    back up to the project's timeline. The href must point at /project/<name>.
    """
    html = render_report(_report(project="demo"))
    assert 'href="/project/demo"' in html
    assert "← demo" in html


def test_not_found_has_back_link_to_index():
    """The 404 page offers a "← Back to projects" link to the index.

    Why this matters: a stale/bad link should not be a dead end — the viewer needs
    a way home from the error page.
    """
    html = render_not_found("nope")
    assert 'href="/"' in html
    assert "← Back to projects" in html


def test_format_ts_humanizes_a_utc_iso_string():
    """A well-formed ISO-8601 UTC string becomes a readable UTC-pinned form.

    Why this matters: this is the core of the helper — "2026-06-18T14:32:00+00:00"
    must render as "Jun 18 2026, 14:32 UTC", staying in UTC (not local time) so the
    displayed time is stable across the machine that wrote it and the one reading it.
    """
    assert _format_ts("2026-06-18T14:32:00+00:00") == "Jun 18 2026, 14:32 UTC"


def test_format_ts_keeps_utc_for_a_nonzero_offset():
    """A timestamp with a non-UTC offset is converted to UTC, not shown as-stored.

    Why this matters: the discipline is UTC-only. An input carrying e.g. +02:00 must
    be normalized to UTC (14:32+02:00 -> 12:32 UTC), never displayed in its source
    offset or in the reader's local time.
    """
    assert _format_ts("2026-06-18T14:32:00+02:00") == "Jun 18 2026, 12:32 UTC"


def test_format_ts_fails_safe_on_garbage():
    """An unparseable timestamp is returned unchanged instead of raising.

    Why this matters: a malformed/unexpected stored value must degrade to the raw
    string rather than crash the whole page render (fail-safe contract).
    """
    assert _format_ts("not-a-timestamp") == "not-a-timestamp"


def test_report_humanizes_timestamps_not_raw_iso():
    """render_report shows the humanized time, not the raw ISO string verbatim.

    Why this matters: the helper must actually be wired into the view — the readable
    form should appear and the raw ISO (with its "T" and offset) should not.
    """
    html = render_report(
        _report(
            generated_at="2026-06-18T14:32:00+00:00",
            ingested_at="2026-06-18T14:35:00+00:00",
        )
    )
    assert "Jun 18 2026, 14:32 UTC" in html      # generated_at, humanized
    assert "Jun 18 2026, 14:35 UTC" in html      # ingested_at, humanized
    assert "2026-06-18T14:32:00+00:00" not in html  # raw ISO not shown verbatim


def test_project_humanizes_timestamp():
    """render_project shows each report's generated_at humanized, not raw ISO.

    Why this matters: the history list link text must also be humanized so the
    timeline reads cleanly, with the raw ISO not leaking through.
    """
    html = render_project("demo", [_report(generated_at="2026-06-18T09:05:00+00:00")])
    assert "Jun 18 2026, 09:05 UTC" in html
    assert "2026-06-18T09:05:00+00:00" not in html


def test_project_section_count_badge_plural():
    """A report with multiple sections shows "· N sections" in the history row.

    Why this matters: the badge lets a reader gauge a report's heft at a glance;
    the multi-section case must pluralize.
    """
    html = render_project(
        "demo",
        [_report(sections=[["A", "x"], ["B", "y"], ["C", "z"]])],
    )
    assert "3 sections" in html


def test_project_section_count_badge_singular():
    """A report with exactly one section shows "1 section" (not "1 sections").

    Why this matters: correct pluralization — the singular boundary is the easy bug
    to ship, so it gets its own assertion.
    """
    html = render_project("demo", [_report(sections=[["A", "x"]])])
    assert "1 section" in html
    assert "1 sections" not in html


def test_project_section_count_badge_flat_body():
    """A report with no sections shows "· flat body" in the history row.

    Why this matters: a sectionless intake push should read as "flat body" rather
    than "0 sections", matching how the report view renders the flat case.
    """
    html = render_project("demo", [_report(sections=[])])
    assert "flat body" in html


def test_report_empty_participants_shows_guard():
    """A report with no participants shows "(no recipients)", not a dangling "to ".

    Why this matters: joining an empty list yields "", which would render as "to  ·"
    — a confusing dangling label. The guard makes the empty case explicit.
    """
    html = render_report(_report(participants=[]))
    assert "(no recipients)" in html
    assert "to  ·" not in html  # no dangling "to " with empty join


def test_css_has_focus_outline():
    """The page CSS includes a visible a:focus outline rule.

    Why this matters: keyboard navigation was invisible (no focus ring). The rule
    must be present in every page's inline stylesheet.
    """
    html = render_not_found("anything")  # any view embeds the shared _PAGE_CSS
    assert "a:focus" in html


# --- C2: comments section on the report page ----------------------------------


def _comment(**overrides):
    """A comment dict (comments_for's shape) with defaults, overridable.

    Why:
        render_report's comments section reads author/body/created_at; defaulting them
        keeps each test to the one field it exercises (e.g. inject _XSS into just the
        body) instead of restating the dict (DRY).
    """
    comment = {
        "id": 1,
        "report_id": 1,
        "author": "Alex",
        "body": "Looks good.",
        "created_at": "2026-06-18T14:32:00+00:00",
    }
    comment.update(overrides)
    return comment


def test_report_renders_the_comment_form():
    """The report page includes a POST form to /report/<id>/comment with a textarea.

    Why this matters: the form is the whole inbound surface in the view — it must
    target the right route for this report's id and offer a body textarea, so a
    supervisor can actually comment back.
    """
    html = render_report(_report(id=7), comments=[])
    assert 'action="/report/7/comment"' in html
    assert 'method="post"' in html
    assert "<textarea" in html


def test_report_comments_empty_state():
    """A report with no comments shows a friendly empty-state, not a blank section.

    Why this matters: a report nobody has commented on yet should read as "No comments
    yet" rather than an empty gap, while still showing the form below it.
    """
    html = render_report(_report(), comments=[])
    assert "No comments yet" in html


def test_report_shows_comment_content_humanized():
    """A comment's author, body, and humanized timestamp all appear on the page.

    Why this matters: this is the read half of the loop — a stored comment must
    surface with its author and text, and the timestamp humanized (UTC-pinned), not as
    raw ISO, matching how report timestamps render.
    """
    html = render_report(
        _report(), comments=[_comment(author="Sam", body="Nice progress.")]
    )
    assert "Sam" in html
    assert "Nice progress." in html
    assert "Jun 18 2026, 14:32 UTC" in html        # humanized created_at
    assert "2026-06-18T14:32:00+00:00" not in html  # raw ISO not shown verbatim


def test_report_anonymous_comment_placeholder():
    """A comment whose author was omitted ("") shows an "anonymous" byline.

    Why this matters: the name is optional, stored as "" when omitted; the view must
    render a neutral placeholder rather than a blank byline so the comment still reads
    cleanly.
    """
    html = render_report(_report(), comments=[_comment(author="")])
    assert "anonymous" in html


def test_report_comment_body_is_escaped():
    """A <script> in a comment body is rendered escaped, never as a live tag.

    Why this matters: a comment is attacker-influenced text on an access-controlled but
    shared page — the classic stored-XSS vector. Escaping is the guarantee it is shown
    as inert text and can never execute in a viewer's browser.
    """
    html = render_report(_report(), comments=[_comment(body=_XSS)])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_report_comment_author_is_escaped():
    """A <script> in a comment author is escaped.

    Why this matters: the author is self-entered free text, just as injectable as the
    body, so it must go through the same escape path — both axes of a comment are
    dynamic.
    """
    html = render_report(_report(), comments=[_comment(author=_XSS)])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
