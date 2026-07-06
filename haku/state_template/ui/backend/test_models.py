"""Models the validate-state gate checks — item docs, the improvements backlog, run manifests.

The bug class these guard: a malformed authored file silently parsing into a wrong shape, or a bad
enum/out-of-range value slipping through (items/, memory/improvements/, and runs/ are hand/
Haku-authored, parsed live at read time / in CI, never build-checked).
"""

from __future__ import annotations

import pytest
import pytest_bazel
from models import ImprovementDoc, ItemDoc, ItemStatus, RunManifest, ToolRequestDoc
from pydantic import ValidationError

# --- item frontmatter (items/<slug>.md) ---------------------------------------


def test_item_doc_parses_minimal_and_optional_fields():
    doc = ItemDoc.model_validate({"title": "t", "value": 35, "status": "snoozed", "snoozed_until": "2027-01-15"})
    assert doc.status is ItemStatus.SNOOZED
    assert doc.snoozed_until is not None
    assert doc.snoozed_until.year == 2027
    assert doc.deadline is None
    # No id/dedup_key/source/action — the slug is the identity, affordances live in the body.
    assert not hasattr(doc, "id")


def test_item_doc_rejects_out_of_range_value_and_bad_status():
    with pytest.raises(ValidationError):
        ItemDoc.model_validate({"title": "t", "value": 250, "status": "open"})
    with pytest.raises(ValidationError):
        ItemDoc.model_validate({"title": "t", "value": 10, "status": "bogus"})


# --- improvements / friction backlog ------------------------------------------


def test_improvement_doc_parses_class_alias():
    doc = ImprovementDoc.model_validate(
        {"kind": "improvement", "class": "idea", "title": "t", "weight": "high", "status": "recommend", "summary": "s"}
    )
    assert doc.doc_class == "idea"
    assert doc.weight == "high"


def test_improvement_doc_rejects_bad_weight_kind_and_class():
    base = {"kind": "improvement", "class": "idea", "title": "t", "weight": "high", "status": "x"}
    for bad in ({"weight": "huge"}, {"kind": "note"}, {"class": "bug"}):
        with pytest.raises(ValidationError):
            ImprovementDoc.model_validate({**base, **bad})


# --- run manifests (runs/<date>/<ulid>.md frontmatter) ------------------------
# The runs surface is composed on the frontend now (client.ts reads each run's frontmatter + prose
# body over the tree+blobs proxy); this schema stays the floor those manifests must satisfy.


def test_run_manifest_accepts_prose_changes_seen_and_created_action():
    # Real manifests carry prose where a count would mislead, and surfaces that were created.
    m = RunManifest.model_validate(
        {
            "run_id": "x",
            "sources": [{"source": "ducktape-git", "changes_seen": "2 commits, neither touches base"}],
            "propagation": [{"change": "c", "surfaces": [{"surface": "s", "action": "created"}]}],
        }
    )
    assert m.sources[0].changes_seen == "2 commits, neither touches base"
    assert m.propagation[0].surfaces[0].action == "created"


def test_run_manifest_rejects_bad_propagation_action():
    # CI schema floor: an unknown propagation action is a hard error.
    with pytest.raises(ValidationError):
        RunManifest.model_validate(
            {"run_id": "x", "propagation": [{"change": "c", "surfaces": [{"surface": "s", "action": "bogus"}]}]}
        )


# --- console-approved tool requests (tool_requests/<id>.yaml) -----------------


def test_tool_request_doc_accepts_precise_authored_call():
    doc = ToolRequestDoc.model_validate(
        {
            "state_request_id": "2026-07-thrive-box-grocy-stock-add",
            "server_id": "grocy-sf",
            "tool_name": "stock_add",
            "title": "Add arrived Thrive box items to Grocy",
            "rationale": "The box has arrived and the products are known.",
            "arguments": {"items": [{"product_id": 123, "amount": 1}]},
        }
    )
    assert doc.server_id == "grocy-sf"
    assert doc.tool_name == "stock_add"
    assert doc.arguments["items"][0]["product_id"] == 123


def test_tool_request_doc_requires_server_tool_title_and_state_id():
    with pytest.raises(ValidationError):
        ToolRequestDoc.model_validate({"server_id": "grocy", "tool_name": "stock_add", "title": "Missing id"})
    with pytest.raises(ValidationError):
        ToolRequestDoc.model_validate({"state_request_id": "req", "tool_name": "stock_add", "title": "Missing server"})
    with pytest.raises(ValidationError):
        ToolRequestDoc.model_validate({"state_request_id": "req", "server_id": "grocy", "title": "Missing tool"})
    with pytest.raises(ValidationError):
        ToolRequestDoc.model_validate({"state_request_id": "req", "server_id": "grocy", "tool_name": "stock_add"})


if __name__ == "__main__":
    pytest_bazel.main()
