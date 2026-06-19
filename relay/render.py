# =============================================================================
# relay/render.py
# -----------------------------------------------------------------------------
# Responsible for: Turning stored report data into the read-only dashboard's HTML.
# Role in project: The presentation layer of the hosted half. These are PURE
#                  functions: they take already-fetched data (the dicts the store
#                  returns) and return an HTML string. They never touch the
#                  database, the network, or request state — which keeps them
#                  trivially testable and keeps all I/O in server.py.
# Security (non-negotiable): EVERY dynamic value is passed through html.escape
#                  before it enters the HTML. The stored content is redacted, but it
#                  is still arbitrary text — a commit message can literally contain
#                  "<script>" — so escaping is what guarantees the dashboard renders
#                  data as inert text, never as markup. Only the static structure
#                  and the inline CSS below are un-escaped.
# Why no template engine / JS: the views are a handful of small, static-structured
#                  pages. Stdlib f-strings + html.escape meet the need with zero
#                  dependencies and nothing to audit beyond "is every value escaped",
#                  which is the open-source-simplicity bar.
# =============================================================================

from __future__ import annotations

import html
import urllib.parse
from datetime import datetime, timezone

# A small inline stylesheet. Inline (not a served static file) keeps the relay a
# single self-contained process with no static-asset routing — the simplest thing
# that gives the dashboard a readable layout.
_PAGE_CSS = """
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.5; }
header a { font-weight: bold; text-decoration: none; }
/* Visible focus ring for keyboard navigation: without this, tabbing through
   links gives no on-screen indication of focus (an accessibility gap). */
a:focus { outline: 2px solid #4af; outline-offset: 2px; }
h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
h2 { font-size: 1.1rem; margin-top: 1.5rem; }
.meta { color: #777; font-size: 0.9rem; }
ul.list { list-style: none; padding: 0; }
ul.list li { padding: 0.4rem 0; border-bottom: 1px solid #8884; }
pre { white-space: pre-wrap; word-wrap: break-word; background: #8881;
      padding: 0.75rem; border-radius: 4px; }
.empty { color: #777; font-style: italic; }
""".strip()


def _esc(value: object) -> str:
    """HTML-escape any value for safe inclusion in markup (text or attribute).

    Args:
        value: Any value (stringified first), e.g. a report body, a project name,
            or an integer id/count.

    Returns:
        The value as an HTML-escaped string, with quotes escaped too.

    Why:
        This is the single chokepoint that makes the dashboard XSS-safe: callers
        route every dynamic value through it, so the rule is just "nothing dynamic
        reaches the HTML without _esc". quote=True also escapes " and ', so the
        same function is safe inside an attribute value (e.g. an href).
    """
    return html.escape(str(value), quote=True)


def _url(value: object) -> str:
    """Percent-encode a value for use as a single URL path segment.

    Args:
        value: The segment to encode (e.g. a project name or a report id).

    Returns:
        A percent-encoded string with NOTHING left unencoded (safe="") — so a name
        containing "/", a space, or "?" becomes one safe segment.

    Why:
        A project name is user-controlled and can contain characters that would
        otherwise break the path or change routing (a "/" would look like another
        segment). Encoding the whole thing keeps each link pointing at exactly one
        project/report. The result is still passed through _esc by the caller before
        landing in an href, so it is both URL- and HTML-safe.
    """
    return urllib.parse.quote(str(value), safe="")


def _format_ts(iso: str) -> str:
    """Render a stored ISO-8601 UTC timestamp as a readable, UTC-pinned string.

    Args:
        iso: A stored timestamp string, e.g. "2026-06-18T14:32:00+00:00".

    Returns:
        A human-readable form like "Jun 18 2026, 14:32 UTC". If the input cannot
        be parsed, the original string is returned UNCHANGED (fail-safe).

    Why:
        Raw ISO strings are precise but noisy to read. We humanize them for the
        dashboard, but a report is a cross-machine artifact: it is generated on one
        machine and read on another, so the displayed time must be stable and
        machine-independent. We therefore convert to UTC and format with a FIXED
        English month table (strftime("%b") is locale-dependent, so it is avoided) —
        never local time and never locale formatting. Failing safe (return the input)
        means a malformed or unexpected timestamp degrades to the raw string rather
        than raising and breaking the whole page render.
    """
    try:
        # fromisoformat handles the stored "+00:00" offset. We force UTC so an input
        # carrying any other offset is still displayed in UTC, not as-stored.
        parsed = datetime.fromisoformat(iso).astimezone(timezone.utc)
    except (ValueError, TypeError):
        # Fail safe: an unparseable value is shown verbatim rather than crashing the
        # render. The caller still escapes it on the way into HTML.
        return iso

    # A fixed, locale-independent month table. Using this instead of strftime("%b")
    # keeps the output identical regardless of the host machine's locale.
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    month = months[parsed.month - 1]  # month is 1-indexed; the table is 0-indexed.
    return f"{month} {parsed.day:02d} {parsed.year}, {parsed.hour:02d}:{parsed.minute:02d} UTC"


def _page(title: str, body_html: str) -> str:
    """Wrap pre-built body HTML in the shared page scaffold.

    Args:
        title: The page <title> text (escaped here).
        body_html: The page's inner HTML. ASSUMED already-safe: the caller has
            escaped every dynamic value it contains. _page does not (and cannot)
            re-escape it, since it is markup by this point.

    Returns:
        A complete HTML document string.

    Why:
        One scaffold for every view keeps the chrome (doctype, charset, the inline
        stylesheet, the home link) defined once (DRY). The contract is explicit: the
        ONLY raw HTML passed in is what a render_* function assembled from escaped
        parts plus static structure.
    """
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_PAGE_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '<header><a href="/">Orion</a></header>\n'
        "<main>\n"
        f"{body_html}\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def render_index(projects: list[dict]) -> str:
    """Render the dashboard home: every project with reports, most recent first.

    Args:
        projects: The list_projects() output — dicts of project/report_count/
            latest_generated_at.

    Returns:
        A complete HTML page.

    Why:
        The entry point a viewer lands on. An empty list renders a friendly
        empty-state rather than a bare page, so a fresh relay (nothing pushed yet)
        explains itself instead of looking broken.
    """
    if not projects:
        body = (
            "<h1>Projects</h1>\n"
            "<p class='empty'>No reports yet. Enable a [relay] in your "
            "orion.toml and run a report to see it here.</p>"
        )
        return _page("Orion — projects", body)

    items = []
    for project in projects:
        name = project["project"]
        href = "/project/" + _url(name)
        items.append(
            f'<li><a href="{_esc(href)}">{_esc(name)}</a> '
            f"<span class='meta'>{_esc(project['report_count'])} report(s) · "
            f"last {_esc(project['latest_generated_at'])}</span></li>"
        )
    body = "<h1>Projects</h1>\n<ul class='list'>\n" + "\n".join(items) + "\n</ul>"
    return _page("Orion — projects", body)


def render_project(project_name: str, reports: list[dict]) -> str:
    """Render one project's report history, newest first.

    Args:
        project_name: The project the history is for (shown as the heading).
        reports: The history() output for that project (may be empty).

    Returns:
        A complete HTML page.

    Why:
        The middle view: pick a project, see its timeline of reports, click into one.
        An empty list (an unknown project, or one that has never pushed) is a clean
        empty-state, not a 404 — we cannot cheaply tell "never existed" from "no
        reports yet", and both mean the same thing to a viewer.
    """
    heading = f"<h1>{_esc(project_name)}</h1>"
    if not reports:
        body = heading + "\n<p class='empty'>No reports for this project yet.</p>"
        return _page(f"Orion — {project_name}", body)

    items = []
    for report in reports:
        href = "/report/" + _url(report["id"])
        # Section-count badge: gives a reader a sense of each report's heft at a
        # glance. A report with no sections is a flat-body intake push.
        section_count = len(report["sections"])
        if section_count == 0:
            heft = "flat body"
        elif section_count == 1:
            heft = "1 section"  # singular — never "1 sections".
        else:
            heft = f"{section_count} sections"
        items.append(
            f'<li><a href="{_esc(href)}">{_esc(_format_ts(report["generated_at"]))}</a> '
            f"<span class='meta'>{_esc(report['lane'])} · "
            f"{_esc(report['share_level'])} · {_esc(heft)}</span></li>"
        )
    body = heading + "\n<ul class='list'>\n" + "\n".join(items) + "\n</ul>"
    return _page(f"Orion — {project_name}", body)


def render_report(report: dict) -> str:
    """Render a single report: its metadata and its sections (or flat body).

    Args:
        report: One get() result dict (project, body, sections, participants,
            share_level, lane, generated_at, orion_version, ingested_at).

    Returns:
        A complete HTML page.

    Why:
        The leaf view — the actual update a supervisor reads. When the report has
        per-signal sections we render each as a titled block; when it has none (an
        intake push carries a single body and no sections) we render the flat body
        as one block, mirroring how the chat composer treats "no sections". Bodies
        go in <pre> so the report's line structure survives, with the content
        escaped so that structure can never become markup.
    """
    # Empty-participants guard: with no recipients, joining yields "" and the line
    # would read "to  ·" (a dangling "to "). Show an explicit placeholder instead.
    if report["participants"]:
        recipients = "to " + _esc(", ".join(report["participants"]))
    else:
        recipients = _esc("(no recipients)")

    meta = (
        "<p class='meta'>"
        f"Report #{_esc(report['id'])} · "
        f"Generated {_esc(_format_ts(report['generated_at']))} · "
        f"lane {_esc(report['lane'])} · "
        f"{_esc(report['share_level'])} · "
        f"{recipients} · "
        f"Orion {_esc(report['orion_version'])} · "
        f"received {_esc(_format_ts(report['ingested_at']))}"
        "</p>"
    )

    # Breadcrumb back to the project's history. The name is percent-encoded for the
    # href (one safe segment) and escaped for both the href and the display text.
    project_href = "/project/" + _url(report["project"])
    breadcrumb = (
        f'<p class="meta"><a href="{_esc(project_href)}">'
        f"← {_esc(report['project'])}</a></p>"
    )

    if report["sections"]:
        blocks = []
        for title, section_body in report["sections"]:
            blocks.append(
                f"<section><h2>{_esc(title)}</h2>"
                f"<pre>{_esc(section_body)}</pre></section>"
            )
        sections_html = "\n".join(blocks)
    else:
        # No per-signal sections (e.g. an intake push): render the flat body.
        sections_html = f"<section><pre>{_esc(report['body'])}</pre></section>"

    body = (
        f"{breadcrumb}\n<h1>{_esc(report['project'])}</h1>\n{meta}\n{sections_html}"
    )
    return _page(f"Orion — {report['project']} report", body)


def render_not_found(message: str = "Not found.") -> str:
    """Render the 404 page.

    Args:
        message: A short reason (e.g. which id/path was not found). Escaped here —
            it often echoes user-controlled input, so it must never be raw.

    Returns:
        A complete HTML page (the caller sends it with a 404 status).

    Why:
        A bad or stale link is an expected case, so it gets a friendly page with a
        way home rather than a bare error. The message is escaped because it
        typically contains the requested path/id, which is attacker-controllable.
    """
    # Back-link to the index so a dead-end 404 still offers a way home. The href is
    # the static root, so no escaping/encoding of dynamic input is needed here.
    body = (
        f"<h1>Not found</h1>\n<p>{_esc(message)}</p>\n"
        '<p class="meta"><a href="/">← Back to projects</a></p>'
    )
    return _page("Orion — not found", body)
