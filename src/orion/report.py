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
from orion.collectors.tasks import ChecklistItem
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
        checklist: The project's CURRENT checklist (open + done items) as of this
            report, or None when the project has no checklist enabled. This is the
            dashboard's "live checklist" signal — current state, not a delta — and is
            OPTIONAL: None means "omit from the wire entirely" (a producer without the
            feature), while an empty tuple means "checklist enabled, but no items" (a
            meaningful state that should clear any stale live checklist on the relay).

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
    generated_at: str
    orion_version: str
    # Defaulted last so existing positional construction stays valid; () is an
    # immutable default, safe on a frozen dataclass.
    sections: tuple[tuple[str, str], ...] = ()
    # Optional and defaulted None so every existing call site and stored/round-tripped
    # blob stays valid; None vs () carries the "feature off" vs "enabled but empty"
    # distinction documented above.
    checklist: tuple[ChecklistItem, ...] | None = None


def build_report(
    project: ProjectConfig,
    body: str,
    lane: str,
    generated_at: str,
    sections: tuple[tuple[str, str], ...] = (),
    checklist: tuple[ChecklistItem, ...] | None = None,
) -> ReportBlob:
    """Assemble a ReportBlob from a project and a finished body.

    Args:
        project: The project's config (for name + recipients).
        body: The redacted report text (from the LLM or a passthrough).
        lane: The lane that produced the body ("raw" or "structured").
        generated_at: ISO 8601 UTC timestamp.
        sections: The ordered (title, body) sections the body was assembled from,
            each already twice-redacted. Defaults to empty for callers (like
            `intake`) that have only a single body and no per-signal sections.
        checklist: The project's current checklist (already redacted), or None when
            the project has no checklist enabled. Defaulted so existing call sites
            are unchanged.

    Returns:
        A populated ReportBlob.

    Why:
        This is intentionally lane-agnostic: Phase 2's structured collectors call
        it with the same signature, passing their already-formatted text as body.
        Centralizing assembly means participant extraction and version stamping
        happen in exactly one place. `sections` and `checklist` are additive
        (defaulted) so every existing call site keeps working unchanged.
    """
    participants = tuple(r.name for r in project.recipients)
    return ReportBlob(
        project=project.name,
        participants=participants,
        share_level=project.share_level,
        lane=lane,
        body=body,
        generated_at=generated_at,
        orion_version=__version__,
        sections=sections,
        checklist=checklist,
    )


def serialize_blob(blob: ReportBlob) -> str:
    """Serialize a ReportBlob to the portable JSON string that crosses the relay seam.

    Args:
        blob: The ReportBlob to serialize. Its tuple fields (`participants` and the
            ordered `sections` pairs) are converted to JSON arrays.

    Returns:
        A JSON string carrying the blob's fields. Keys are sorted so the same blob
        always serializes to the same bytes (deterministic wire format). The optional
        `checklist` key is present only when the blob carries one (None → omitted),
        keeping it back-compatible with receivers that predate the field.

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
        "generated_at": blob.generated_at,
        "orion_version": blob.orion_version,
        # Each section is an ordered (title, body) pair; JSON has no tuple, so emit
        # a two-element list and preserve section order (the list itself is ordered).
        "sections": [[title, body] for title, body in blob.sections],
    }
    # Optional field: emit it ONLY when the producer attached a checklist. None ⇒
    # omit the key entirely (a producer without the feature), so a receiver that
    # predates the field is unaffected; an empty list ⇒ "enabled but no items", a
    # real state the relay uses to clear a stale live checklist. Each item is a named
    # object (see serialize_checklist_item) so the done-state reads explicitly.
    if blob.checklist is not None:
        payload["checklist"] = [serialize_checklist_item(item) for item in blob.checklist]
    # sort_keys → byte-stable output for a given blob; the seam needs a predictable
    # wire format more than it needs human-friendly field order.
    return json.dumps(payload, sort_keys=True)


def serialize_checklist_item(item: ChecklistItem) -> dict:
    """Serialize one checklist item to its portable wire dict.

    Args:
        item: The ChecklistItem to serialize.

    Returns:
        A dict carrying "text" and "done" always, plus "due_date", "key", and "group"
        each ONLY when the item has one (None → that key omitted).

    Why:
        Two wire paths emit a checklist — the report blob (serialize_blob) and the
        dedicated /checklist push (cli._checklist_payload) — so the per-item shape is
        defined ONCE here to keep them in lockstep. Each optional field follows the same
        None-omitted rule as the top-level `checklist` key: an item without them serializes
        to exactly the old {text, done}, so a receiver predating a field is unaffected.
        `due_date` is the forward-looking deadline; `key` is the forward-store's stable
        identity (the tracker's bare title); `group` is the milestone the relay rolls items
        up by (E2 Inc 3 Unit 5). Single-sourcing made each additive field a one-line change
        in one place.
    """
    payload: dict = {"text": item.text, "done": item.done}
    if item.due_date is not None:
        payload["due_date"] = item.due_date
    if item.key is not None:
        payload["key"] = item.key
    if item.group is not None:
        payload["group"] = item.group
    return payload
