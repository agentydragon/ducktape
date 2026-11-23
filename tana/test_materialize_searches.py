import json
import sys

import pytest

from tana.export.materialize_searches import main


def _doc(node_id, *children, **props):
    return {"id": node_id, "children": children, "props": props}


@pytest.fixture
def export_path(tmp_path):
    path = tmp_path / "export.json"
    docs = [
        _doc(
            "direct",
            "stale",
            _docType="search",
            _metaNodeId="direct-meta",
            searchContextNode="scope",
            _ownerId="parent",
        ),
        _doc("direct-meta", "view-holder", "direct-query"),
        _doc("direct-query", "SYS_A15", "any-match", "skip-cancelled", _docType="tuple"),
        _doc("any-match", "SYS_A42", "SYS_T103", "parent-field", _docType="tuple"),
        _doc("skip-cancelled", "SYS_A43", "cancelled-text", _docType="tuple"),
        _doc("parent-field", "project-key", "parent-value", _docType="tuple"),
        _doc("nested", "event", _docType="search", _metaNodeId="nested-meta", name="View search"),
        _doc("nested-meta", "view-holder"),
        _doc("view-holder", "view-key", "view", _docType="tuple"),
        _doc("view", "nested-query", _docType="viewDef"),
        _doc("nested-query", "SYS_A15", "tag", _docType="tuple"),
        _doc("empty", _docType="search"),
        _doc("scope", "event", "cancelled", "field-match"),
        _doc("event", _docType="event", name="Work review"),
        _doc("event-tags", "SYS_A13", "tag", _docType="tuple", _ownerId="event"),
        _doc("tag", _docType="tagDef", name="Project tag"),
        _doc("tag-tags", "SYS_A13", "tag-type", _docType="tuple", _ownerId="tag"),
        _doc("tag-type", name="Tag"),
        _doc("SYS_A13"),
        _doc("SYS_T103"),
        _doc("cancelled", _docType="event", name="Cancelled"),
        _doc("cancelled-text", name="Cancelled"),
        _doc("field-match", "field-value", name="Matching project"),
        _doc("field-value", "project-key", "parent", _docType="tuple"),
        _doc("project-key", name="Project"),
        _doc("parent-value", name="PARENT"),
        _doc("parent", name="Synthetic project"),
        _doc("outside", _docType="event", name="Outside scope"),
        _doc("stale", name="Previously matched"),
    ]
    path.write_text(json.dumps({"docs": docs}), encoding="utf-8")
    return path


def test_list_searches(export_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["materialize_searches", str(export_path), "--list"])
    main()
    assert (
        capsys.readouterr().out
        == "Found 3 search nodes:\n  - <unnamed> (direct)\n  - View search (nested)\n  - <unnamed> (empty)\n"
    )


def test_materialize_all(export_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["materialize_searches", str(export_path), "--all"])
    main()
    sections = capsys.readouterr().out.strip().split("-" * 60)
    direct, nested, empty, tail = sections
    assert direct.startswith("Processing 3 search nodes...")
    assert "Search: direct\nID: direct\nExpression: " in direct
    assert "Stored results: 1\nMaterialized results: 2" in direct
    assert "Missing from materialized (1):\n  - Previously matched (stale)" in direct
    assert "Extra in materialized (2):" in direct
    assert "  - Work review (event)" in direct
    assert "  - Matching project (field-match)" in direct
    assert "Search: View search\nID: nested\nExpression: kind='tag' tag_id='tag'" in nested
    assert "Stored results: 1\nMaterialized results: 1" in nested
    assert "Stored and materialized results match!" in nested
    assert "Search: empty\nID: empty\nExpression: <none>" in empty
    assert "Stored results: 0\nMaterialized results: 0" in empty
    assert not tail


def test_materialize_one(export_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["materialize_searches", str(export_path), "--search-id", "nested"])
    main()
    assert capsys.readouterr().out.startswith("\nSearch: View search\nID: nested\n")
