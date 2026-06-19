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
# Why no template engine: the views are a handful of small, static-structured pages.
#                  Stdlib f-strings + html.escape meet the need with zero dependencies
#                  and nothing to audit beyond "is every value escaped", the
#                  open-source-simplicity bar.
# On JavaScript: the dashboard is server-rendered and fully functional with NO JS. The
#                  one script (_PAGE_JS, inline) is PROGRESSIVE ENHANCEMENT only — it
#                  rewrites absolute timestamps to relative ("2 days ago") in the
#                  browser. It writes via textContent (never innerHTML) and reads only
#                  server-escaped values, so it adds no injection surface: the
#                  every-value-escaped audit above still fully covers safety.
# =============================================================================

from __future__ import annotations

import html
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

# The dashboard displays timestamps in California time. We pin a FIXED IANA zone
# (not the server's local time, not the reader's) so the displayed time is the same
# regardless of where the relay runs or who is viewing — and zoneinfo applies the
# DST rules, so it reads PDT (UTC-7) in summer and PST (UTC-8) in winter
# automatically. Created once at import (ZoneInfo is cached). Requires the `tzdata`
# package on platforms without a system tz database (slim Docker, native Windows);
# it is a declared dependency, so a missing-tzdata failure can't reach production.
_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")

# Input limits for a supervisor comment (C2). They live HERE — the dependency-free
# module — because BOTH sides need them: render.py uses them as the comment form's
# client-side `maxlength` hint, and server.py imports them to ENFORCE the limit on the
# decoded fields (the authoritative check). Keeping one definition avoids the two
# numbers drifting apart. server.py already imports from render.py, so this direction
# introduces no import cycle (the reverse would).
MAX_COMMENT_BODY_CHARS = 4_000
MAX_AUTHOR_CHARS = 200

# A small inline stylesheet. Inline (not a served static file) keeps the relay a
# single self-contained process with no static-asset routing — the simplest thing
# that gives the dashboard a readable, "refined-minimal" layout. The palette is
# driven by CSS custom properties (design tokens) defined once for light and once
# for dark (prefers-color-scheme), so colors stay coherent and future theming is a
# token change rather than a sweep. color-scheme also makes native form controls and
# scrollbars match the active scheme.
_PAGE_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #6b6b6b;
  --accent: #2563eb;
  --border: #e4e4e7;
  --surface: #f6f6f7;
  --radius: 8px;
  --maxw: 720px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181c;
    --fg: #e8e8ea;
    --muted: #9aa0a6;
    --accent: #6ea8fe;
    --border: #2a2d33;
    --surface: #1e2127;
  }
}
* { box-sizing: border-box; }
body {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  max-width: var(--maxw); margin: 0 auto; padding: 2.5rem 1.25rem 4rem;
  line-height: 1.6; color: var(--fg); background: var(--bg);
  -webkit-font-smoothing: antialiased;
}
header { margin-bottom: 2rem; }
header a {
  font-weight: 600; font-size: 1.05rem; letter-spacing: -0.01em;
  text-decoration: none; color: var(--fg);
}
header a:hover { color: var(--accent); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
/* Visible focus ring for keyboard nav (accessibility). :focus-visible shows it for
   keyboard users without ringing on every mouse click. */
a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px;
}
h1 { font-size: 1.6rem; line-height: 1.25; margin: 0 0 0.35rem; letter-spacing: -0.02em; }
h2 { font-size: 1.05rem; margin: 2rem 0 0.5rem; letter-spacing: -0.01em; }
p { margin: 0.5rem 0; }
.meta { color: var(--muted); font-size: 0.875rem; }
/* List views (index, project history): each row is a link on the left with quiet
   meta on the right; wraps cleanly on narrow screens. */
ul.list { list-style: none; padding: 0; margin: 1.25rem 0 0; }
ul.list li {
  display: flex; flex-wrap: wrap; gap: 0.2rem 0.75rem;
  align-items: baseline; justify-content: space-between;
  padding: 0.7rem 0; border-bottom: 1px solid var(--border);
}
ul.list li a { font-weight: 500; }
/* Report bodies: preserve line structure; sit on a subtle bordered surface. */
section { margin: 1.25rem 0; }
pre {
  white-space: pre-wrap; word-wrap: break-word;
  background: var(--surface); border: 1px solid var(--border);
  padding: 0.9rem 1rem; border-radius: var(--radius);
  font-size: 0.9rem; line-height: 1.55; overflow-x: auto;
}
.empty { color: var(--muted); font-style: italic; }
/* Comment thread: stack each comment (byline above body) rather than the
   left/right row layout the other lists use. */
ul.list.comments li { display: block; padding: 1rem 0; }
ul.list.comments .meta { display: block; margin-bottom: 0.35rem; }
ul.list.comments pre { margin: 0; }
/* Comment form. */
form { margin-top: 1.25rem; }
form label { display: block; font-size: 0.875rem; color: var(--muted); margin-bottom: 1rem; }
input[type="text"], textarea {
  display: block; width: 100%; margin-top: 0.3rem; font: inherit;
  color: var(--fg); background: var(--bg);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 0.5rem 0.65rem;
}
textarea { resize: vertical; }
button {
  font: inherit; font-weight: 500; cursor: pointer; color: #ffffff;
  background: var(--accent); border: 1px solid transparent;
  border-radius: var(--radius); padding: 0.5rem 1.1rem;
}
button:hover { filter: brightness(1.05); }
""".strip()

# A single, static progressive-enhancement script. It rewrites each <time datetime>
# element (emitted by _time_tag) to a relative label ("2 days ago") and keeps the
# absolute California time as the hover title. PROGRESSIVE ENHANCEMENT: with JS off,
# the server-rendered absolute text simply stands — the page is fully functional
# without this. SECURITY: it reads only the datetime attribute (server-escaped) and
# the existing textContent, and writes via textContent (never innerHTML), so it adds
# NO injection surface — the "every dynamic value escaped on the server" rule is
# untouched. Inline (like _PAGE_CSS) to keep the relay a single self-contained
# process with no static-asset routing.
_PAGE_JS = """
(function () {
  function relative(then, now) {
    var secs = Math.round((now - then) / 1000);
    var future = secs < 0;
    secs = Math.abs(secs);
    var units = [
      ["year", 31536000], ["month", 2592000], ["week", 604800],
      ["day", 86400], ["hour", 3600], ["minute", 60]
    ];
    for (var i = 0; i < units.length; i++) {
      var n = Math.floor(secs / units[i][1]);
      if (n >= 1) {
        var label = n + " " + units[i][0] + (n === 1 ? "" : "s");
        return future ? "in " + label : label + " ago";
      }
    }
    return "just now";
  }
  var now = Date.now();
  var nodes = document.querySelectorAll("time[datetime]");
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var t = Date.parse(el.getAttribute("datetime"));
    if (isNaN(t)) { continue; }           // leave an unparseable value as rendered
    if (!el.title) { el.title = el.textContent; }  // absolute Pacific time on hover
    el.textContent = relative(t, now);             // textContent => no HTML injection
  }
})();
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
    """Render a stored ISO-8601 timestamp as a readable California-time string.

    Args:
        iso: A stored timestamp string, e.g. "2026-06-18T14:32:00+00:00".

    Returns:
        A human-readable form like "Jun 18 2026, 07:32 PDT". If the input cannot
        be parsed, the original string is returned UNCHANGED (fail-safe).

    Why:
        Raw ISO strings are precise but noisy to read. We humanize them and display
        them in California time (where the user and supervisors are). We convert to a
        FIXED zone (America/Los_Angeles), NOT the server's local time and NOT the
        reader's — so the displayed time is identical no matter where the relay runs
        or who views it — and zoneinfo applies DST, so the abbreviation/offset is
        correct year-round (PDT in summer, PST in winter; read live via tzname()).
        The month uses a FIXED English table (strftime("%b") is locale-dependent, so
        it is avoided); the tz abbreviation comes from the IANA data, not the locale,
        so it too is stable. Failing safe (return the input) means a malformed or
        unexpected timestamp degrades to the raw string rather than raising and
        breaking the whole page render.
    """
    try:
        # fromisoformat handles the stored "+00:00" offset; astimezone converts that
        # absolute instant into California wall-clock time (DST applied by zoneinfo).
        parsed = datetime.fromisoformat(iso).astimezone(_DISPLAY_TZ)
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
    # tzname() returns the zone's abbreviation for THIS instant ("PDT" or "PST"),
    # from the IANA database — so the label always matches the offset just applied.
    abbrev = parsed.tzname()
    return (
        f"{month} {parsed.day:02d} {parsed.year}, "
        f"{parsed.hour:02d}:{parsed.minute:02d} {abbrev}"
    )


def _time_tag(iso: str) -> str:
    """Wrap a stored timestamp in a <time> element: machine ISO + human Pacific text.

    Args:
        iso: The stored ISO-8601 timestamp string.

    Returns:
        '<time datetime="<escaped ISO>">escaped human Pacific time</time>'. Both the
        attribute and the visible text are escaped via _esc.

    Why:
        The carrier for the relative-timestamp progressive enhancement. The visible
        text is the absolute California time (_format_ts), so the page reads correctly
        with NO JavaScript. The datetime attribute carries the machine-readable instant
        so _PAGE_JS can compute a relative label ("2 days ago") in the browser without
        re-parsing the human string. The raw ISO now appears in the page — but only
        inside this machine-readable attribute, escaped — which is exactly its purpose;
        the human-facing text is still the humanized form.
    """
    return f'<time datetime="{_esc(iso)}">{_esc(_format_ts(iso))}</time>'


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
        # Script at end of body so the DOM it enhances already exists. It is the only
        # JS on the page and is purely additive (see _PAGE_JS).
        f"<script>{_PAGE_JS}</script>\n"
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
            f'<li><a href="{_esc(href)}">{_time_tag(report["generated_at"])}</a> '
            f"<span class='meta'>{_esc(report['lane'])} · "
            f"{_esc(report['share_level'])} · {_esc(heft)}</span></li>"
        )
    body = heading + "\n<ul class='list'>\n" + "\n".join(items) + "\n</ul>"
    return _page(f"Orion — {project_name}", body)


def render_report(report: dict, comments: list[dict] | None = None) -> str:
    """Render a single report: its metadata, its sections (or flat body), comments.

    Args:
        report: One get() result dict (project, body, sections, participants,
            share_level, lane, generated_at, orion_version, ingested_at).
        comments: The comments_for() list for this report (each a dict with author,
            body, created_at). Defaults to None → treated as empty, so existing
            one-argument callers/tests keep working (the comments section is additive).

    Returns:
        A complete HTML page.

    Why:
        The leaf view — the actual update a supervisor reads, and now ALSO where they
        comment back (C2). When the report has per-signal sections we render each as a
        titled block; when it has none (an intake push carries a single body and no
        sections) we render the flat body as one block, mirroring how the chat composer
        treats "no sections". Bodies go in <pre> so the report's line structure
        survives, with the content escaped so that structure can never become markup.
        The comments section (list + post form) is appended last via a helper, keeping
        this function focused on the report itself.
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
        f"Generated {_time_tag(report['generated_at'])} · "
        f"lane {_esc(report['lane'])} · "
        f"{_esc(report['share_level'])} · "
        f"{recipients} · "
        f"Orion {_esc(report['orion_version'])} · "
        f"received {_time_tag(report['ingested_at'])}"
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

    comments_html = _render_comments(report["id"], comments or [])

    body = (
        f"{breadcrumb}\n<h1>{_esc(report['project'])}</h1>\n{meta}\n{sections_html}\n"
        f"{comments_html}"
    )
    return _page(f"Orion — {report['project']} report", body)


def _render_comments(report_id: object, comments: list[dict]) -> str:
    """Render the comments section: the existing thread plus the post form.

    Args:
        report_id: The report's id — used both to build the form's POST action and
            (it is already trusted, but) escaped on the way into the href anyway.
        comments: The report's comments, oldest-first (comments_for's order). May be
            empty, in which case a friendly empty-state stands in for the list.

    Returns:
        An HTML fragment (heading + list/empty-state + form), every dynamic value
        escaped. Assembled here, not inline in render_report, to keep that function
        readable.

    Why:
        This is the C2 inbound surface's view half. EVERY dynamic value — the author,
        the body, the timestamp, and the id inside the form action — is routed through
        _esc (and the id additionally through _url for the path), because a comment is
        attacker-influenced text: the stored-XSS defense for the whole feature lives in
        this escaping. The form is a plain HTML POST (no JS): the browser submits it,
        the server validates + stores + 303-redirects back here. maxlength mirrors the
        server's caps as a courtesy, but the server is the authoritative check.
    """
    if comments:
        items = []
        for comment in comments:
            # An omitted name was stored as "", so show a neutral placeholder rather
            # than a blank byline. The placeholder is static (not user input).
            who = comment["author"] if comment["author"] else "anonymous"
            items.append(
                f'<li><span class="meta">{_esc(who)} · '
                f'{_time_tag(comment["created_at"])}</span>'
                f'<pre>{_esc(comment["body"])}</pre></li>'
            )
        thread_html = '<ul class="list comments">\n' + "\n".join(items) + "\n</ul>"
    else:
        thread_html = "<p class='empty'>No comments yet.</p>"

    # The id is trusted (an int from the store), but encode + escape it anyway so the
    # action attribute is built by the same safe path as every other dynamic href.
    action = _esc("/report/" + _url(report_id) + "/comment")
    form = (
        f'<form method="post" action="{action}">\n'
        '<p><label>Name (optional)<br>'
        f'<input type="text" name="author" maxlength="{MAX_AUTHOR_CHARS}"></label></p>\n'
        '<p><label>Comment<br>'
        f'<textarea name="body" rows="4" required '
        f'maxlength="{MAX_COMMENT_BODY_CHARS}"></textarea></label></p>\n'
        '<p><button type="submit">Post comment</button></p>\n'
        "</form>"
    )
    return f"<h2>Comments</h2>\n{thread_html}\n{form}"


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
