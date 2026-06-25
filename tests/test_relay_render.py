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
from zoneinfo import ZoneInfo

from relay.render import (
    PAGE_CSS_HASH,
    PAGE_JS_HASH,
    _format_ts,
    _headline,
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
