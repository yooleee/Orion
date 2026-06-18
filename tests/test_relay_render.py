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
    assert "2026-06-18T09:00:00+00:00" in html


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
