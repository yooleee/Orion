# =============================================================================
# tests/test_report_serialize.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying serialize_blob produces the portable JSON contract
#                  losslessly.
# Role in project: serialize_blob is the wire format local Orion POSTs to a relay
#                  and the receiver stores/renders. If a field is dropped, mistyped,
#                  or mangled here, every hosted surface downstream is wrong — so we
#                  pin all seven fields and the tuple→array conversions explicitly.
# =============================================================================

import json

from orion import __version__
from orion.collectors.tasks import ChecklistItem
from orion.report import ReportBlob, serialize_blob


def _blob(sections=(), checklist=None):
    """A fully-populated ReportBlob for serialization tests.

    Args:
        sections: The ordered (title, body) pairs to attach. Defaults to empty so a
            test can opt into either the empty- or populated-sections case.
        checklist: The optional live checklist (tuple of ChecklistItem) or None.
            Defaults to None so the field is absent unless a test opts in.

    Why:
        Centralizes blob construction so each test varies only what it is checking
        rather than restating every field.
    """
    return ReportBlob(
        project="demo",
        participants=("Alex", "Sam"),
        share_level="detailed",
        lane="raw",
        body="Shipped the relay seam.",
        generated_at="2026-06-17T00:00:00+00:00",
        orion_version=__version__,
        sections=sections,
        checklist=checklist,
    )


def test_serialize_blob_round_trips_all_fields():
    """Serializing then reconstructing a populated blob yields the original.

    Why this matters: this is the core promise of the wire contract — nothing is
    lost crossing the seam. We serialize, parse the JSON back, rebuild a ReportBlob
    (converting the JSON arrays back to the tuples the dataclass uses), and assert
    it equals what we started with. Covering a blob with sections exercises all
    seven fields at once.
    """
    original = _blob(sections=(("Code activity", "Did X."), ("Tasks", "Did Y.")))

    parsed = json.loads(serialize_blob(original))
    rebuilt = ReportBlob(
        project=parsed["project"],
        participants=tuple(parsed["participants"]),
        share_level=parsed["share_level"],
        lane=parsed["lane"],
        body=parsed["body"],
        generated_at=parsed["generated_at"],
        orion_version=parsed["orion_version"],
        sections=tuple((title, body) for title, body in parsed["sections"]),
    )

    assert rebuilt == original


def test_serialize_blob_converts_tuples_to_json_arrays():
    """participants and sections land as JSON arrays, not tuples or strings.

    Why this matters: JSON has no tuple type. A receiver in a different language
    (a future Cloudflare Worker, say) only sees arrays, so we pin that the
    conversion happened — participants is a list of names, and each section is a
    two-element [title, body] list in display order.
    """
    parsed = json.loads(serialize_blob(_blob(sections=(("Title", "Body"),))))

    assert parsed["participants"] == ["Alex", "Sam"]
    assert parsed["sections"] == [["Title", "Body"]]


def test_serialize_blob_empty_sections_is_empty_array():
    """A blob with no sections serializes sections to an empty array.

    Why this matters: an `intake` push carries a single body and no per-signal
    sections. The receiver must see an explicit empty list (so it can render the
    body as one section) rather than a missing key — the field must always be
    present and well-typed.
    """
    parsed = json.loads(serialize_blob(_blob(sections=())))

    assert parsed["sections"] == []


def test_serialize_blob_omits_checklist_when_none():
    """A blob with no checklist omits the key entirely (back-compat).

    Why this matters: the checklist is an OPTIONAL field added after the original
    contract. None means "this producer has no checklist," so the key must be absent
    — not an empty list — so a receiver that predates the field, and the validator's
    optional-field handling, both see exactly the old shape.
    """
    parsed = json.loads(serialize_blob(_blob(checklist=None)))

    assert "checklist" not in parsed


def test_serialize_blob_emits_checklist_as_text_done_objects():
    """A checklist serializes to a list of {text, done} objects, in order.

    Why this matters: the live-checklist wire shape is named objects (not positional
    pairs) so the done-state is explicit for any receiver. Order is preserved so the
    rendered list matches the user's file.
    """
    checklist = (
        ChecklistItem(text="Build it", done=True),
        ChecklistItem(text="Ship it", done=False),
    )
    parsed = json.loads(serialize_blob(_blob(checklist=checklist)))

    assert parsed["checklist"] == [
        {"text": "Build it", "done": True},
        {"text": "Ship it", "done": False},
    ]


def test_serialize_blob_empty_checklist_is_present_empty_array():
    """An enabled-but-empty checklist serializes to a present empty array.

    Why this matters: () is meaningfully different from None — it says "checklist
    enabled, currently no items," which the relay uses to CLEAR a stale live
    checklist. So the key must be present (as []), distinguishing it from the
    omitted-when-None case above.
    """
    parsed = json.loads(serialize_blob(_blob(checklist=())))

    assert parsed["checklist"] == []


def test_serialize_blob_round_trips_checklist():
    """Serializing then reconstructing a blob with a checklist yields the original.

    Why this matters: the checklist must survive the seam losslessly, like the other
    fields. We rebuild the ChecklistItem tuple from the parsed {text, done} objects.
    """
    original = _blob(
        checklist=(
            ChecklistItem(text="Open task", done=False),
            ChecklistItem(text="Done task", done=True),
        )
    )

    parsed = json.loads(serialize_blob(original))
    rebuilt_checklist = tuple(
        ChecklistItem(text=item["text"], done=item["done"])
        for item in parsed["checklist"]
    )

    assert rebuilt_checklist == original.checklist


def test_serialize_blob_is_deterministic():
    """The same blob always serializes to the same string.

    Why this matters: sort_keys gives a byte-stable wire format. A predictable
    serialization keeps the contract testable and means a relay could safely
    de-duplicate or checksum payloads without worrying about key-order drift.
    """
    blob = _blob(sections=(("A", "1"),))

    assert serialize_blob(blob) == serialize_blob(blob)


def test_serialize_blob_stamps_orion_version():
    """The serialized payload carries the running Orion version.

    Why this matters: orion_version is the forward-compat handle — a receiver uses
    it to accept, reject, or adapt a payload. It must survive serialization as the
    actual version string, not be dropped.
    """
    parsed = json.loads(serialize_blob(_blob()))

    assert parsed["orion_version"] == __version__
