# =============================================================================
# report.py
# -----------------------------------------------------------------------------
# Responsible for: The portable "summary + metadata" blob that flows from the
#                  pipeline into composing and delivery.
# Role in project: The lane-agnostic contract between "what was produced" and
#                  "how it gets sent." build_report is called the same way whether
#                  the body came from the LLM (raw lane) or a passthrough
#                  (structured lane, Phase 2). Keeping it a plain, serializable
#                  dataclass means a future shared service could ingest it as-is.
# =============================================================================

from __future__ import annotations

import json
from dataclasses import dataclass

from orion import __version__
from orion.config import ProjectConfig


@dataclass(frozen=True)
class ReportBlob:
    """A self-contained, portable progress report plus its metadata.

    Args:
        project: The project name this report is about.
        participants: Names of the people who receive it (explicit, not an
            implicit "me") — keeps the door open for multi-supervisor delivery.
        share_level: The level the body was generated at ("high_level"/"detailed").
        lane: "raw" (an LLM summarized at least part of this run) or "structured"
            (everything passed through) — provenance.
        body: The redacted, channel-agnostic report text.
        source_marker: VESTIGIAL since Phase 2 — always "". Phase 1 stored the
            single git HEAD sha here, but delta markers are now per-collector and
            live in the state store (collector_markers), so no single value is
            meaningful when several signals run. Kept (frozen) for the portable
            blob's shape; see docs/known-issues.md KI-8.
        generated_at: ISO 8601 UTC timestamp of when the report was built.
        orion_version: The Orion version that produced it (forward-compat).
        sections: The ordered (title, body) sections this report is composed of,
            each already twice-redacted, in display order. Carried so the composer
            can build Slack Block Kit / Discord embeds directly instead of
            re-parsing the flattened `body` (B3). Empty for an `intake` push (a
            single pushed body with no per-signal sections), in which case the
            composer renders `body` as one section. `body` stays the canonical
            redacted text and the plain-text fallback, so anything that only needs
            a string keeps working.

    Why:
        Bundling everything needed to send AND to advance state into one frozen
        object means delivery, the composer, and the state writer all read from a
        single source of truth, and nothing downstream has to reach back into the
        collector or config.
    """

    project: str
    participants: tuple[str, ...]
    share_level: str
    lane: str
    body: str
    source_marker: str
    generated_at: str
    orion_version: str
    # Defaulted last so existing positional construction stays valid; () is an
    # immutable default, safe on a frozen dataclass.
    sections: tuple[tuple[str, str], ...] = ()


def build_report(
    project: ProjectConfig,
    body: str,
    lane: str,
    source_marker: str,
    generated_at: str,
    sections: tuple[tuple[str, str], ...] = (),
) -> ReportBlob:
    """Assemble a ReportBlob from a project and a finished body.

    Args:
        project: The project's config (for name + recipients).
        body: The redacted report text (from the LLM or a passthrough).
        lane: The lane that produced the body ("raw" or "structured").
        source_marker: The git HEAD sha covered by this report.
        generated_at: ISO 8601 UTC timestamp.
        sections: The ordered (title, body) sections the body was assembled from,
            each already twice-redacted. Defaults to empty for callers (like
            `intake`) that have only a single body and no per-signal sections.

    Returns:
        A populated ReportBlob.

    Why:
        This is intentionally lane-agnostic: Phase 2's structured collectors call
        it with the same signature, passing their already-formatted text as body.
        Centralizing assembly means participant extraction and version stamping
        happen in exactly one place. `sections` is additive (defaulted) so every
        existing call site keeps working unchanged.
    """
    participants = tuple(r.name for r in project.recipients)
    return ReportBlob(
        project=project.name,
        participants=participants,
        share_level=project.share_level,
        lane=lane,
        body=body,
        source_marker=source_marker,
        generated_at=generated_at,
        orion_version=__version__,
        sections=sections,
    )


def serialize_blob(blob: ReportBlob) -> str:
    """Serialize a ReportBlob to the portable JSON string that crosses the relay seam.

    Args:
        blob: The ReportBlob to serialize. Its tuple fields (`participants` and the
            ordered `sections` pairs) are converted to JSON arrays.

    Returns:
        A JSON string carrying all eight blob fields. Keys are sorted so the same
        blob always serializes to the same bytes (deterministic wire format).

    Why:
        This is the single, explicit definition of Orion's portable report contract
        — the exact shape that local Orion POSTs to a relay and that the receiver
        stores and renders. JSON has no tuple type, so `participants` becomes a list
        and each `(title, body)` section becomes a `[title, body]` list; doing that
        by hand (rather than leaning on json's silent tuple→array coercion) keeps
        the wire shape visible and intentional in one place. `orion_version` rides
        along so a receiver can reject or adapt to a payload from a different
        version. We build the dict field-by-field instead of `dataclasses.asdict`
        precisely so this contract reads as a deliberate interface, not a dump of
        whatever fields the dataclass happens to have.
    """
    payload = {
        "project": blob.project,
        "participants": list(blob.participants),
        "share_level": blob.share_level,
        "lane": blob.lane,
        "body": blob.body,
        "source_marker": blob.source_marker,
        "generated_at": blob.generated_at,
        "orion_version": blob.orion_version,
        # Each section is an ordered (title, body) pair; JSON has no tuple, so emit
        # a two-element list and preserve section order (the list itself is ordered).
        "sections": [[title, body] for title, body in blob.sections],
    }
    # sort_keys → byte-stable output for a given blob; the seam needs a predictable
    # wire format more than it needs human-friendly field order.
    return json.dumps(payload, sort_keys=True)
