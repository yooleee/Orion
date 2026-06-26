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

import base64
import hashlib
import re
from datetime import date
from zoneinfo import ZoneInfo

from relay.render import (
    PAGE_CSS_HASH,
    PAGE_JS_HASH,
    _format_ts,
    _headline,
    _render_checklist,
    _render_milestones,
    render_not_found,
    render_portfolio,
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


def _pcard(**overrides):
    """A portfolio-row dict (latest_report_per_project's shape) with defaults, overridable.

    Why:
        render_portfolio reads five fields per project; defaulting them keeps each test to
        the one field it exercises (e.g. inject _XSS into just latest_body) instead of
        restating the whole dict (DRY) — the same pattern as _report/_comment below.
    """
    row = {
        "project": "demo",
        "report_count": 3,
        "latest_generated_at": "2026-06-18T00:00:00+00:00",
        "latest_report_id": 1,
        "latest_body": "Shipped the seam.",
    }
    row.update(overrides)
    return row


def test_portfolio_lists_projects_with_links():
    """The portfolio home shows each project name and links to its project page.

    Why this matters: the home view's whole job is to be the jump-off point —
    the project name must appear and its link must point at /project/<name>.
    """
    html = render_portfolio([_pcard(project="demo", report_count=3)])
    assert "demo" in html
    assert 'href="/project/demo"' in html
    assert "3 report(s)" in html


def test_portfolio_shows_latest_report_headline():
    """Each card shows the first line of its latest report's body as a headline.

    Why this matters: the headline is what makes the home a glanceable showcase — a
    family member should read "what's happening" per project without clicking in. We use
    only the report's OWN first line (no invented text).
    """
    html = render_portfolio([_pcard(latest_body="Deployed the dashboard.\nmore detail here")])
    assert "Deployed the dashboard." in html
    assert "more detail here" not in html  # only the FIRST line becomes the headline


def test_portfolio_headline_truncates_a_long_first_line():
    """A first line longer than the cap is truncated with an ellipsis.

    Why this matters: a card must stay roughly one line to remain scannable; an overlong
    first line is cut rather than blowing out the layout.
    """
    long_line = "x" * 250
    html = render_portfolio([_pcard(latest_body=long_line)])
    assert "…" in html  # truncation marker present
    assert ("x" * 250) not in html  # the full overlong line never renders


def test_portfolio_headline_is_escaped():
    """A malicious latest-report body is escaped in the card headline (stored-XSS guard).

    Why this matters: the headline is the report's OWN body text, which is
    attacker-influenceable (a commit message can contain "<script>"). It must render as
    inert text, exactly like every other dynamic value on the dashboard.
    """
    html = render_portfolio([_pcard(latest_body=_XSS)])
    assert _XSS not in html  # never appears as a live tag
    assert "&lt;script&gt;" in html


def test_portfolio_omits_headline_when_body_is_empty():
    """A card whose latest body has no usable line renders no headline element.

    Why this matters: the honest fallback is to drop the headline, not show a blank or
    placeholder line — a report with an empty body simply has no one-liner to show.
    """
    html = render_portfolio([_pcard(latest_body="   \n  \n")])
    assert "class='headline'" not in html  # the headline paragraph is omitted entirely


def test_portfolio_wraps_last_activity_in_a_time_tag():
    """Each card's last-activity time is a <time datetime> element (for the relative-time JS).

    Why this matters: the home shows "2 days ago" via the inline enhancement, which only
    applies to <time datetime> nodes — so the card must emit one (an upgrade over the old
    index's raw timestamp string).
    """
    html = render_portfolio([_pcard(latest_generated_at="2026-06-18T00:00:00+00:00")])
    assert '<time datetime="2026-06-18T00:00:00+00:00">' in html


def test_portfolio_card_shows_checklist_badge():
    """A project with a live checklist shows an "X/Y done" badge on its card.

    Why this matters: this is the founding cross-project glance — family seeing, at a
    glance, how far along each project is. When the store surfaces the counts, the card
    must render them.
    """
    html = render_portfolio([_pcard(checklist_done=2, checklist_total=3)])
    assert "2/3 done" in html


def test_portfolio_card_omits_badge_when_no_checklist():
    """A project without a checklist (counts absent/None) shows no badge.

    Why this matters: the badge is opt-in — a project without the feature must look
    exactly as it did before, not show a stray "0/0" or an empty element. The default
    _pcard carries no checklist keys, so this pins the .get()-driven omission.
    """
    html = render_portfolio([_pcard()])
    # The badge element is the precise signal (the word "done" also appears in the
    # page's static CSS comment, so we assert on the class, not the bare word).
    assert "class='checklist-badge'" not in html


def test_portfolio_card_omits_badge_when_checklist_empty():
    """An enabled-but-empty checklist (0 of 0) shows no badge either.

    Why this matters: a 0/0 badge is noise, not signal. `if total` treats an empty
    checklist the same as no checklist for the card — the block only appears once
    there is something to count.
    """
    html = render_portfolio([_pcard(checklist_done=0, checklist_total=0)])
    assert "class='checklist-badge'" not in html


def test_portfolio_renders_checklist_only_card_without_a_report():
    """A checklist-only card (no report) renders with its badge, link, time, and 0 reports.

    Why this matters: this is the slice's payoff — the applications tracker has a live
    checklist but zero reports, so its card carries None for latest_body/
    latest_generated_at and falls back to checklist_updated_at for its last-activity
    time. render_portfolio must NOT crash on those Nones (neither _headline nor _time_tag
    is None-safe), must omit the headline, show "0 report(s)", render the badge, and link
    to the project page.
    """
    card = _pcard(
        project="applications",
        report_count=0,
        latest_body=None,
        latest_generated_at=None,
        latest_report_id=None,
        checklist_updated_at="2026-06-25T10:00:00+00:00",
        checklist_done=1,
        checklist_total=2,
    )
    html = render_portfolio([card])

    assert 'href="/project/applications"' in html
    assert "0 report(s)" in html
    assert "1/2 done" in html
    # Last-activity time falls back to the checklist's updated_at.
    assert '<time datetime="2026-06-25T10:00:00+00:00">' in html
    # No report body → no headline element (honest omission, not a blank line).
    assert "class='headline'" not in html


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


def test_report_renders_live_checklist_done_and_open():
    """The report page shows the project's live checklist with done/open state.

    Why this matters: this is the leaf-view delivery of the checklist signal — both
    what is done AND what is still open/planned, with a count. We pin the heading, the
    count, both item texts, and the done/open state classes the styling keys off.
    """
    checklist = [
        {"text": "Wire the relay", "done": True},
        {"text": "Render the dashboard", "done": False},
    ]
    html = render_report(_report(), checklist=checklist)

    assert "Current checklist" in html
    assert "1/2 done" in html
    assert "Wire the relay" in html
    assert "Render the dashboard" in html
    assert 'class="done"' in html   # the completed item carries the done class
    assert 'class="open"' in html   # the open item carries the open class


def test_report_checklist_item_text_is_escaped():
    """A checklist item's text is HTML-escaped (it is arbitrary user text).

    Why this matters: checklist items are user-authored, so a task literally named
    "<script>..." must render inert — the same XSS guarantee the report body and
    section content have. This is the structured-lane content reaching a new surface.
    """
    html = render_report(_report(), checklist=[{"text": _XSS, "done": False}])
    assert "&lt;script&gt;" in html
    assert "<script>alert" not in html


def test_report_omits_checklist_when_none_or_empty():
    """No checklist block renders when the project has none (None) or it is empty.

    Why this matters: the block is additive — a report for a project without the
    feature (None) or with an enabled-but-empty checklist ([]) must look exactly as
    before, with no stray "Current checklist" heading.
    """
    assert "Current checklist" not in render_report(_report(), checklist=None)
    assert "Current checklist" not in render_report(_report(), checklist=[])
    # And the default call (no checklist arg) is unaffected — back-compat.
    assert "Current checklist" not in render_report(_report())


# --- empty states -------------------------------------------------------------


def test_portfolio_empty_state():
    """A portfolio with no projects renders a friendly empty-state, not a bare page.

    Why this matters: a fresh relay (nothing pushed yet) should explain itself
    rather than look broken.
    """
    html = render_portfolio([])
    assert "No reports yet" in html


def test_project_empty_state():
    """A project with no reports renders an empty-state (still a 200-style page).

    Why this matters: an unknown or never-pushed project is a normal case, shown as
    a clean empty page rather than an error.
    """
    html = render_project("ghost", [])
    assert "ghost" in html
    assert "No reports for this project yet" in html


def test_project_renders_live_checklist_block():
    """The project page shows the live checklist block (the near-real-time watch surface).

    Why this matters: the project page is the persistent home a viewer keeps open to
    watch checklist edits land. We pass the get_checklist() shape and assert the block,
    its count, and both items render with their done/open state.
    """
    checklist = [
        {"text": "Wire it", "done": True},
        {"text": "Render it", "done": False},
    ]
    html = render_project(
        "demo", [_report(id=7)], checklist=checklist
    )
    assert "Current checklist" in html
    assert "1/2 done" in html
    assert "Wire it" in html
    assert "Render it" in html


def test_project_shows_checklist_even_with_no_reports():
    """The checklist block renders on a project page that has no reports yet.

    Why this matters: the live checklist is current state, independent of report
    history. A project being watched before its first report must still show its
    checklist (and the empty-state note for the timeline below it).
    """
    html = render_project("demo", [], checklist=[{"text": "Plan", "done": False}])
    assert "Current checklist" in html
    assert "Plan" in html
    assert "No reports for this project yet" in html  # timeline empty-state still shown


def test_project_omits_checklist_when_none_or_empty():
    """No checklist block renders when the project has none (None) or it is empty.

    Why this matters: the block is additive — a project without the feature must look
    exactly as before, with no stray "Current checklist" heading.
    """
    assert "Current checklist" not in render_project("demo", [_report()], checklist=None)
    assert "Current checklist" not in render_project("demo", [_report()], checklist=[])
    # And the default call (no checklist arg) is unaffected — back-compat.
    assert "Current checklist" not in render_project("demo", [_report()])


def test_project_checklist_item_text_is_escaped():
    """A checklist item's text is HTML-escaped on the project page (XSS guard).

    Why this matters: checklist items are user text reaching a new surface; a task
    named "<script>..." must render inert here exactly as on the report page.
    """
    html = render_project("demo", [_report()], checklist=[{"text": _XSS, "done": False}])
    assert "&lt;script&gt;" in html
    assert "<script>alert" not in html


# --- XSS: every dynamic value must be escaped ---------------------------------


def test_report_body_is_escaped():
    """A <script> in the report body is rendered escaped, never as a live tag.

    Why this matters: stored content is redacted but still arbitrary text — a commit
    message can contain HTML. Escaping is the guarantee that such content is shown
    as inert text and can never execute in a viewer's browser.
    """
    html = render_report(_report(sections=[], body=_XSS))
    assert _XSS not in html  # the raw <script> payload never appears unescaped
    assert "&lt;script&gt;" in html


def test_report_section_title_and_body_are_escaped():
    """A <script> in a section title OR body is escaped.

    Why this matters: sections are dynamic on both axes; both the title and the body
    must be escaped, not just one.
    """
    html = render_report(_report(sections=[[_XSS, _XSS]]))
    assert _XSS not in html  # the raw <script> payload never appears unescaped
    assert "&lt;script&gt;" in html


def test_report_participant_name_is_escaped():
    """A <script> in a participant name is escaped.

    Why this matters: participant names come from config and flow into the metadata
    line; they are dynamic and must be escaped like everything else.
    """
    html = render_report(_report(participants=[_XSS]))
    assert _XSS not in html  # the raw <script> payload never appears unescaped
    assert "&lt;script&gt;" in html


def test_portfolio_project_name_is_escaped():
    """A malicious project name is escaped in the portfolio (text AND its href).

    Why this matters: the project name appears both as link text and inside the
    href; both must be safe. The percent-encoded href must not contain a raw "<",
    and the visible text must be escaped.
    """
    html = render_portfolio([_pcard(project=_XSS, report_count=1)])
    assert _XSS not in html  # the raw <script> payload never appears unescaped
    assert "&lt;script&gt;" in html


def test_headline_takes_first_non_empty_line():
    """_headline skips leading blank lines and returns the first line with content.

    Why this matters: a report body may start with blank lines; the headline should be the
    first MEANINGFUL line, stripped, not an empty string from a leading newline.
    """
    assert _headline("\n  \nReal first line\nsecond") == "Real first line"


def test_headline_empty_body_returns_empty_string():
    """_headline on a body with no content returns "" so the caller omits the headline.

    Why this matters: the omit-the-line fallback depends on "" signalling "nothing to
    show" — an all-whitespace body must collapse to the empty string, not a blank line.
    """
    assert _headline("   \n\n  ") == ""


def test_headline_truncates_only_past_the_limit():
    """_headline truncates with an ellipsis past the cap, but leaves a boundary-length line whole.

    Why this matters: the truncation must be off-by-one-correct — a line EXACTLY at the cap
    is shown in full (no ellipsis), and only a longer line is cut. Pins the boundary.
    """
    exact = "a" * 10
    assert _headline(exact, limit=10) == exact  # exactly at the cap: untouched
    assert _headline("a" * 11, limit=10) == "a" * 10 + "…"  # one over: truncated


def test_not_found_message_is_escaped():
    """The 404 message (which echoes the requested path) is escaped.

    Why this matters: the not-found message typically contains attacker-controllable
    input (the bad path/id), so it is a real XSS vector if rendered raw.
    """
    html = render_not_found(f"Unknown path {_XSS!r}.")
    assert _XSS not in html  # the raw <script> payload never appears unescaped
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


def test_format_ts_humanizes_to_california_summer_pdt():
    """A summer UTC instant renders in California time as PDT (UTC-7).

    Why this matters: this is the core of the helper — "2026-06-18T14:32:00+00:00"
    (June, so DST is in effect) must render as "Jun 18 2026, 07:32 PDT": converted to
    California wall-clock (14:32 UTC - 7h = 07:32) and labelled PDT, not UTC.
    """
    assert _format_ts("2026-06-18T14:32:00+00:00") == "Jun 18 2026, 07:32 PDT"


def test_format_ts_handles_dst_winter_pst():
    """A winter UTC instant renders as PST (UTC-8), proving DST is applied live.

    Why this matters: California time is not a fixed offset — this is exactly what the
    tzdata dependency buys. A January instant ("2026-01-15T14:32:00+00:00") must shift
    by 8 hours and read "Jan 15 2026, 06:32 PST", while the June case above shifts by 7
    and reads PDT. If the zone were hardcoded to a fixed offset, one of these would be
    wrong.
    """
    assert _format_ts("2026-01-15T14:32:00+00:00") == "Jan 15 2026, 06:32 PST"


def test_format_ts_converts_a_nonzero_offset_to_california():
    """A timestamp with a non-UTC offset is normalized to the same absolute instant.

    Why this matters: the input is an absolute instant regardless of the offset it
    carries. "2026-06-18T14:32:00+02:00" is 12:32 UTC, which is 05:32 in California
    (PDT, -7), so it must render "Jun 18 2026, 05:32 PDT" — never its source offset or
    the reader's local time.
    """
    assert _format_ts("2026-06-18T14:32:00+02:00") == "Jun 18 2026, 05:32 PDT"


def test_format_ts_fails_safe_on_garbage():
    """An unparseable timestamp is returned unchanged instead of raising.

    Why this matters: a malformed/unexpected stored value must degrade to the raw
    string rather than crash the whole page render (fail-safe contract).
    """
    assert _format_ts("not-a-timestamp") == "not-a-timestamp"


def test_format_ts_renders_in_a_configured_non_pacific_zone():
    """A non-default tz argument renders the same instant in THAT zone, not Pacific.

    Why this matters: this is the KI-20 follow-up — the display zone is configurable
    per relay process (orion relay-serve --timezone). The SAME instant
    ("2026-06-18T14:32:00+00:00", i.e. 14:32 UTC) must read differently per zone:
    "Jun 18 2026, 14:32 UTC" in UTC and "Jun 18 2026, 15:32 BST" in London (summer,
    DST -> +1), and neither equals the Pacific "07:32 PDT" the default produces. That
    last inequality is the real assertion: a passed-in zone is genuinely used, not the
    hardcoded module constant.
    """
    instant = "2026-06-18T14:32:00+00:00"
    assert _format_ts(instant, ZoneInfo("UTC")) == "Jun 18 2026, 14:32 UTC"
    # London in June is BST (UTC+1), so 14:32 UTC -> 15:32 BST.
    assert _format_ts(instant, ZoneInfo("Europe/London")) == "Jun 18 2026, 15:32 BST"
    # The configured zone genuinely overrides the Pacific default.
    assert _format_ts(instant, ZoneInfo("UTC")) != _format_ts(instant)


def test_format_ts_defaults_to_pacific_byte_for_byte():
    """Omitting the tz argument is byte-identical to passing the Pacific zone.

    Why this matters: the contract for the configurable-zone change is that existing
    callers (and an omitted --timezone) keep the EXACT historical output. The
    one-argument call must equal the explicit-Pacific call, instant for instant.
    """
    instant = "2026-06-18T14:32:00+00:00"
    assert _format_ts(instant) == _format_ts(instant, ZoneInfo("America/Los_Angeles"))


def test_render_report_renders_timestamps_in_a_configured_zone():
    """render_report threads a non-default tz down into every timestamp it shows.

    Why this matters: the zone must reach the actual view, not just the leaf helper —
    server.py passes self.server.display_tz into render_report, so a London-configured
    relay's report page must show generated_at/ingested_at in BST, and must NOT show
    the Pacific label it would render by default.
    """
    html = render_report(
        _report(
            generated_at="2026-06-18T14:32:00+00:00",
            ingested_at="2026-06-18T14:35:00+00:00",
        ),
        comments=None,
        tz=ZoneInfo("Europe/London"),
    )
    assert "Jun 18 2026, 15:32 BST" in html  # generated_at in London time, not PDT
    assert "Jun 18 2026, 15:35 BST" in html  # ingested_at in London time
    assert "PDT" not in html  # the Pacific default is genuinely overridden


def test_render_project_renders_timestamps_in_a_configured_zone():
    """render_project threads a non-default tz down into each report's <time> text.

    Why this matters: the history list is the other timestamped dashboard view, so the
    same wiring must hold there — a UTC-configured relay shows the timeline in UTC, not
    the hardcoded Pacific zone.
    """
    html = render_project(
        "demo",
        [_report(generated_at="2026-06-18T09:05:00+00:00")],
        tz=ZoneInfo("UTC"),
    )
    assert "Jun 18 2026, 09:05 UTC" in html  # rendered in UTC, the configured zone
    assert "PDT" not in html


def test_report_wraps_timestamps_in_time_tags():
    """render_report shows the humanized time as visible text and the ISO in <time>.

    Why this matters: the helper must be wired into the view as a <time> element — the
    readable California form is the visible text (works with no JS), while the raw ISO
    appears ONLY inside the machine-readable datetime attribute (its purpose: the JS
    relative-timestamp enhancement reads it). So the ISO is present, but in the
    attribute, never as the human-facing text.
    """
    html = render_report(
        _report(
            generated_at="2026-06-18T14:32:00+00:00",
            ingested_at="2026-06-18T14:35:00+00:00",
        )
    )
    assert "Jun 18 2026, 07:32 PDT" in html      # generated_at, humanized to PDT
    assert "Jun 18 2026, 07:35 PDT" in html      # ingested_at, humanized to PDT
    # The ISO lives in the datetime attribute (machine-readable), not the visible text.
    assert '<time datetime="2026-06-18T14:32:00+00:00">' in html
    assert ">2026-06-18T14:32:00+00:00<" not in html  # never as element text content


def test_project_wraps_timestamp_in_time_tag():
    """render_project shows each report's generated_at humanized inside a <time>.

    Why this matters: the history list link text must be humanized so the timeline
    reads cleanly, with the raw ISO carried only in the datetime attribute (for the JS
    enhancement), not shown as the visible link text.
    """
    html = render_project("demo", [_report(generated_at="2026-06-18T09:05:00+00:00")])
    assert "Jun 18 2026, 02:05 PDT" in html  # 09:05 UTC -> 02:05 California (PDT)
    assert '<time datetime="2026-06-18T09:05:00+00:00">' in html
    assert ">2026-06-18T09:05:00+00:00<" not in html  # not the visible text


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


def test_page_includes_progressive_enhancement_script():
    """Every page embeds the inline relative-timestamp script.

    Why this matters: the relative-timestamp enhancement only works if the shared
    scaffold actually ships the script. We assert it is present (a <script> with the
    distinctive time[datetime] selector it queries) on any view — like the CSS, it
    lives in _page so every page gets it.
    """
    html = render_not_found("anything")  # any view embeds the shared scaffold
    assert "<script>" in html
    assert "time[datetime]" in html  # the selector the enhancement queries


def test_csp_hashes_match_the_inline_blocks_the_page_renders():
    """The CSP hashes render.py exposes equal the SHA-256 of the page's actual inline blocks.

    Why this matters: the dashboard's hash-based CSP allowlists its ONE inline <style>
    and ONE inline <script> by hash. If render.py's exposed hash ever disagreed with the
    bytes the page actually emits between the tags, the live CSP would BLOCK the
    dashboard's own CSS/JS — a policy worse than none. This test recomputes the hashes
    the way a browser does (over the exact bytes between the tags) and pins them to the
    constants server.py builds the policy from. It always passes today (both derive from
    the same constant), but it documents and guards the invariant: change the CSS/JS
    without the hash tracking it, and this fails loudly here instead of in production.
    """
    html = render_not_found("anything")  # any view embeds the shared _page scaffold

    # Extract the EXACT bytes between each tag pair — that is what a browser hashes for
    # a hash-source CSP. DOTALL so the multi-line CSS/JS bodies match; non-greedy so we
    # stop at the first closing tag (there is exactly one of each block per page).
    css = re.search(r"<style>(.*?)</style>", html, re.DOTALL).group(1)
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)

    def _sha256_csp(content):
        digest = hashlib.sha256(content.encode("utf-8")).digest()
        return "sha256-" + base64.b64encode(digest).decode("ascii")

    assert _sha256_csp(css) == PAGE_CSS_HASH
    assert _sha256_csp(script) == PAGE_JS_HASH


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


def test_comment_form_shows_name_field_when_not_logged_in():
    """With no author_name (open mode), the form offers the optional free-text name field.

    Why this matters: on a bare loopback relay there is no identity, so a commenter may
    type a name — the form must still present that input, preserving today's behavior.
    """
    html = render_report(_report(id=7), comments=[])  # author_name defaults to None
    assert 'name="author"' in html
    assert "Commenting as" not in html


def test_comment_form_shows_identity_and_drops_name_field_when_logged_in():
    """With author_name set, the form shows "Commenting as <name>" and no name input.

    Why this matters: a logged-in viewer's comment is attributed to their authenticated
    identity server-side, so the form must reflect that — it shows who they are commenting
    as and removes the free-text name field, which would otherwise be silently ignored.
    """
    html = render_report(_report(id=7), comments=[], author_name="alice")
    assert "Commenting as" in html
    assert "alice" in html
    assert 'name="author"' not in html  # the free-text name field is gone
    assert "<textarea" in html          # the comment body field remains


def test_comment_form_escapes_the_authenticated_name():
    """The authenticated name is escaped when rendered into the form (defense).

    Why this matters: even though the name comes from the store, it is rendered into the
    page, so it must go through the same escaping as every other dynamic value — a name
    with markup characters must never become live HTML.
    """
    html = render_report(_report(id=7), comments=[], author_name='<script>x</script>')
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


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
    surface with its author and text, and the timestamp humanized to California time,
    not as raw ISO, matching how report timestamps render.
    """
    html = render_report(
        _report(), comments=[_comment(author="Sam", body="Nice progress.")]
    )
    assert "Sam" in html
    assert "Nice progress." in html
    assert "Jun 18 2026, 07:32 PDT" in html        # 14:32 UTC -> 07:32 California
    # ISO carried only in the <time> datetime attribute, not as visible text.
    assert '<time datetime="2026-06-18T14:32:00+00:00">' in html
    assert ">2026-06-18T14:32:00+00:00<" not in html


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
    assert _XSS not in html  # the raw <script> payload never appears unescaped
    assert "&lt;script&gt;" in html


def test_report_comment_author_is_escaped():
    """A <script> in a comment author is escaped.

    Why this matters: the author is self-entered free text, just as injectable as the
    body, so it must go through the same escape path — both axes of a comment are
    dynamic.
    """
    html = render_report(_report(), comments=[_comment(author=_XSS)])
    assert _XSS not in html  # the raw <script> payload never appears unescaped
    assert "&lt;script&gt;" in html


# --- forward-looking deadlines on the checklist (E2 Inc 3) ---------------------------
# These call _render_checklist directly with a FIXED today so the overdue/due-soon
# classification is deterministic (no real clock); the date 2026-06-26 is the reference.

_FIXED_TODAY = date(2026, 6, 26)


def _cl_item(text, done=False, due_date=None):
    """Build a checklist item dict, attaching due_date only when given (the real shape)."""
    item = {"text": text, "done": done}
    if due_date is not None:
        item["due_date"] = due_date
    return item


def test_render_checklist_shows_open_item_due_date():
    """An open item with a deadline renders its due date as an enhanceable <time>.

    Why this matters: the due date is the visible forward signal. We pin the human date
    text (no-JS fallback) and that it rides a <time datetime> so the existing relative-time
    JS can turn it into "in N days" client-side.
    """
    html = _render_checklist([_cl_item("Apply", due_date="2026-07-17")], today=_FIXED_TODAY)
    assert "Jul 17, 2026" in html
    assert 'class="due"' in html
    assert "<time datetime=" in html


def test_render_checklist_overdue_item_gets_class_and_marker():
    """A past-due open item gets the 'overdue' li class and a ⚠ marker (legible sans CSS).

    Why this matters: overdue is the strongest signal. The class drives the red styling and
    the ⚠ keeps it legible even if the stylesheet is blocked (function before looks).
    """
    html = _render_checklist([_cl_item("Late", due_date="2026-06-12")], today=_FIXED_TODAY)
    assert 'class="open overdue"' in html
    assert "⚠" in html


def test_render_checklist_due_soon_item_gets_at_risk_class_without_marker():
    """A within-7-days open item gets the softer 'at-risk' class and NO ⚠ marker.

    Why this matters: due-soon is a lighter warning than overdue. It shares the at-risk
    treatment but should not carry the overdue ⚠, so the two states stay distinguishable.
    """
    html = _render_checklist([_cl_item("Soon", due_date="2026-06-29")], today=_FIXED_TODAY)
    assert 'class="open at-risk"' in html
    assert "⚠" not in html


def test_render_checklist_done_item_is_never_flagged_or_dated():
    """A done item shows neither an at-risk class nor a due date, even with a past deadline.

    Why this matters: a finished item is not "at risk", and its deadline is moot — surfacing
    either would be misleading noise. It must render exactly as a plain done item.
    """
    html = _render_checklist(
        [_cl_item("Did it", done=True, due_date="2026-06-01")], today=_FIXED_TODAY
    )
    assert "overdue" not in html
    assert "at-risk" not in html
    assert 'class="due"' not in html
    assert 'class="done"' in html


def test_render_checklist_item_without_due_date_is_unchanged():
    """An open item with no deadline renders as the plain open item it always was.

    Why this matters: the deadline is additive — a project that never sets one must look
    exactly as it did before Inc 3 (no due span, no status class).
    """
    html = _render_checklist([_cl_item("No date")], today=_FIXED_TODAY)
    assert 'class="due"' not in html
    assert 'class="open"' in html


def test_render_portfolio_shows_at_risk_badge():
    """A card whose project has at-risk items renders an 'N at risk' badge.

    Why this matters: the portfolio is the at-a-glance home; the badge is how a viewer sees
    a project is slipping without opening it. The count comes precomputed from the store.
    """
    html = render_portfolio(
        [_pcard(checklist_done=1, checklist_total=4, checklist_at_risk=2)]
    )
    assert "2 at risk" in html


def test_render_portfolio_omits_at_risk_badge_when_zero_or_none():
    """No badge when the at-risk count is 0 or None (no derivation / nothing at risk).

    Why this matters: a project with nothing at risk — or one whose count was not computed
    — must look exactly as it did before, never showing a "0 at risk" badge.
    """
    none_html = render_portfolio([_pcard(checklist_at_risk=None)])
    zero_html = render_portfolio([_pcard(checklist_at_risk=0)])
    # Assert the badge ELEMENT is absent (a loose "at risk" substring would also match the
    # phrase inside the page's CSS comment, which ships on every page).
    assert "<p class='at-risk-badge'>" not in none_html
    assert "<p class='at-risk-badge'>" not in zero_html


# --- slipping (E2 Inc 3 Unit 4) ------------------------------------------------------


def test_render_checklist_marks_a_slipping_open_item():
    """An open item whose key is in slipping_keys gets the ↘ slipping marker; others don't.

    Why this matters: the per-item slipping treatment is membership in the precomputed set,
    matched on the item's identity (key, else text). An item not in the set is untouched.
    """
    slipping = {"text": "App - In progress", "done": False, "key": "App"}
    other = {"text": "Other - In progress", "done": False, "key": "Other"}
    html = _render_checklist(
        [slipping, other], today=_FIXED_TODAY, slipping_keys=frozenset({"App"})
    )
    assert "↘ slipping" in html
    assert 'class="open slipping"' in html  # the slipping item's li
    # Exactly one item is marked (the other is plain "open" with no slipping span).
    assert html.count("↘ slipping") == 1


def test_render_checklist_done_item_is_never_marked_slipping():
    """A done item is never marked slipping, even if its key is in the set.

    Why this matters: slipping is an OPEN-work signal; a finished item's bumpy history is
    moot. The render gates on not-done, matching is_slipping's own done short-circuit.
    """
    item = {"text": "App", "done": True, "key": "App"}
    html = _render_checklist([item], today=_FIXED_TODAY, slipping_keys=frozenset({"App"}))
    assert "slipping" not in html  # _render_checklist emits no CSS, so this is the marker only


def test_render_project_marks_slipping_from_observations():
    """render_project derives the slipping set from observations and marks the live item.

    Why this matters: end-to-end of the project-page surface — a postponed deadline in the
    observation history (2026-07-01 → 2026-07-15) makes the live item slip. Postponement is
    today-independent, so this is deterministic without injecting a date.
    """
    checklist = [{"text": "App - In progress", "done": False, "key": "App"}]
    observations = [
        {"item_key": "App", "due_date": "2026-07-01", "done": False, "observed_at": "2026-06-20T00:00:00+00:00"},
        {"item_key": "App", "due_date": "2026-07-15", "done": False, "observed_at": "2026-06-25T00:00:00+00:00"},
    ]
    html = render_project("demo", [], checklist=checklist, observations=observations)
    assert "↘ slipping" in html


def test_render_portfolio_shows_slipping_badge():
    """A card whose project has slipping items renders an "N slipping" badge.

    Why this matters: the portfolio surfaces slippage at a glance; the count is precomputed
    by the store. The "↘" marker keeps it legible without the stylesheet.
    """
    html = render_portfolio([_pcard(checklist_slipping=3)])
    assert "3 slipping" in html


def test_render_portfolio_omits_slipping_badge_when_zero_or_none():
    """No slipping badge when the count is 0 or None (not computed / nothing slipping).

    Why this matters: a healthy project shows no badge; assert the ELEMENT is absent (a loose
    "slipping" substring would also match the page's CSS comment).
    """
    none_html = render_portfolio([_pcard(checklist_slipping=None)])
    zero_html = render_portfolio([_pcard(checklist_slipping=0)])
    assert "<p class='slipping-badge'>" not in none_html
    assert "<p class='slipping-badge'>" not in zero_html


# --- derived milestones (E2 Inc 3 Unit 5) --------------------------------------------


def _ms(group, done=0, total=1, at_risk=0, nearest_due=None):
    """Build one milestone dict (derive.milestones()'s shape) with defaults, overridable.

    Why: _render_milestones renders five fields per row; defaulting keeps each test to the
    field it exercises instead of restating the whole dict (DRY), like _pcard/_report.
    """
    return {
        "group": group,
        "done": done,
        "total": total,
        "at_risk": at_risk,
        "nearest_due": nearest_due,
    }


def test_render_milestones_shows_group_progress_due_and_at_risk():
    """A milestone row shows its group, "M/N done", "next due <date>", and the at-risk count.

    Why this matters: this is the at-a-glance roll-up. All four facts must surface, with the
    date humanized (no-JS-friendly) and the at-risk count in its own span so the amber tint
    (shared with the checklist) applies to just the number.
    """
    html = _render_milestones(
        [_ms("Applications", done=1, total=4, at_risk=2, nearest_due="2026-07-04")]
    )
    assert "<section class='milestones'>" in html
    assert "<h2>Milestones</h2>" in html
    assert "Applications" in html
    assert "1/4 done" in html
    assert "next due Jul 4, 2026" in html
    assert "<span class='at-risk'>2 at risk</span>" in html


def test_render_milestones_omits_date_and_at_risk_clauses_when_absent():
    """A group with no open deadline and nothing at risk shows only "M/N done".

    Why this matters: the live to-do tables use year-less dates (nearest_due None), so a real
    milestone often has only progress. The optional clauses must drop cleanly — no dangling
    "next due" with no date, no "0 at risk".
    """
    html = _render_milestones([_ms("Non-Application To-Do", done=0, total=8)])
    assert "0/8 done" in html
    assert "next due" not in html
    assert "<span class='at-risk'>" not in html


def test_render_milestones_escapes_group_name():
    """A milestone group name (user heading text) is escaped, never live markup.

    Why this matters: the group comes from a tracker heading — arbitrary user text — so a
    "<script>" in a heading must render inert, the same stored-XSS defense as everywhere else.
    """
    html = _render_milestones([_ms(_XSS, done=0, total=1)])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_milestones_empty_renders_nothing():
    """No milestones → "" (the caller's join drops the section entirely).

    Why this matters: a project without a structured tracker has no groups; it must take the
    no-section path, never an empty "Milestones" heading.
    """
    assert _render_milestones([]) == ""


def test_render_project_shows_milestones_above_the_checklist():
    """render_project derives milestones from the grouped checklist and places them first.

    Why this matters: end-to-end of the project-page surface, and the settled placement — the
    summary-first roll-up sits ABOVE the per-item checklist. Group/progress/next-due are
    today-independent, so this is deterministic without injecting a date.
    """
    checklist = [
        {"text": "App A", "done": True, "group": "Applications", "due_date": "2026-07-04"},
        {"text": "App B", "done": False, "group": "Applications"},
    ]
    html = render_project("demo", [], checklist=checklist)
    assert "<section class='milestones'>" in html
    assert "1/2 done" in html
    # Placement: the milestones section comes before the checklist section.
    assert html.index("<section class='milestones'>") < html.index("<section class='checklist'>")


def test_render_portfolio_shows_next_milestone_hint():
    """A card whose project has a soonest-due milestone renders a "Next: <group> by <date>" line.

    Why this matters: the home's forward hint points a viewer at the section due next without
    opening the project. The group + date come precomputed from the store as nearest_milestone.
    """
    html = render_portfolio(
        [_pcard(nearest_milestone={"group": "Applications", "nearest_due": "2026-06-12"})]
    )
    assert "<p class='milestone-hint'>" in html
    assert "Applications" in html
    assert "Jun 12, 2026" in html


def test_render_portfolio_omits_milestone_hint_when_none():
    """No hint when nearest_milestone is None or the key is absent (no milestone / no today).

    Why this matters: a project without a grouped+dated milestone must look exactly as before;
    assert the ELEMENT is absent (a loose "milestone" substring matches the CSS comment).
    """
    none_html = render_portfolio([_pcard(nearest_milestone=None)])
    absent_html = render_portfolio([_pcard()])  # _pcard has no nearest_milestone key at all
    assert "<p class='milestone-hint'>" not in none_html
    assert "<p class='milestone-hint'>" not in absent_html
