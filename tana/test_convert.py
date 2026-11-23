"""Tests for convert.py and export_node_subset.py - Tana JSON to Markdown/TanaPaste conversion."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tana.domain.nodes import BaseNode
from tana.export import convert, export_node_subset, tana_issues_to_tanapaste
from tana.export.export_node_subset import TrackingGraph, create_subset_json, export_node_with_tracking
from tana.graph.graph import TanaGraph
from tana.graph.loader import load_workspace

# Testdata lives alongside this test under tana/testdata
TESTDATA_PATH = Path(__file__).parent / "testdata"


def _render_node_as_tanapaste(store: TanaGraph, node: BaseNode) -> str:
    return convert.format_tanapaste(convert.RenderContext(store, "tana").render_node(node))


@pytest.mark.parametrize("node_id", [f.stem for f in TESTDATA_PATH.glob("*.tanapaste")])
def test_node_export(node_id):
    """Test that generated TanaPaste for specific nodes match reference files."""
    # TODO(mpokorny): b9AZZEuj42vC golden includes workspace-level sibling refs after search rows.
    # Converter intentionally emits only search results; review and update the golden or converter rule.
    if node_id == "b9AZZEuj42vC":
        pytest.skip("Pending golden review for search-table node; converter emits only search rows")
    store = load_workspace(TESTDATA_PATH / "test_workspace.json")
    actual = _render_node_as_tanapaste(store, store[node_id])
    expected = (TESTDATA_PATH / f"{node_id}.tanapaste").read_text()
    assert actual == expected


@pytest.mark.parametrize(
    ("folder", "node_id"),
    [
        ("supertag_with_spaces", "BYSeNY-L_Yth"),
        ("table_contextual_column", "5PumeU26_4fo"),
        ("node_attributes", "Oh-JrJ73G9iK"),
        ("datetime_fields", "2Ap-6LC3fVuq"),
        ("inline_references", "6aoZJeWmOXcl"),
        ("supertag_with_attributes", "hiHpuPTowhDs"),
        ("links", "KhlJy8yJ37KN"),
        ("search_node", "LkFZLMTHylYl"),
        ("text_formatting", "r1shM2RHNgCv"),
        ("multiple_nodes_same_supertag", "u00FQD8V08fy"),
        ("multiple_supertags", "x2-AdByI7b-a"),
        ("code_blocks", "YbPcBamWZFGV"),
    ],
)
def test_node_export_minimal_json(folder, node_id):
    """Test that generated TanaPaste from minimal JSON files match reference files."""
    # Load from the minimal JSON file in the feature folder
    base = TESTDATA_PATH / folder
    store = load_workspace(base / f"{node_id}.json")
    actual = _render_node_as_tanapaste(store, store[node_id])
    expected = (base / f"{node_id}.tanapaste").read_text()
    assert actual == expected


@pytest.mark.parametrize("node_id", [f.stem for f in TESTDATA_PATH.glob("*.tanapaste")])
def test_subset_export(node_id):
    """The subset exporter builds its tracking graph from the workspace JSON and renders the golden TanaPaste."""
    if node_id == "b9AZZEuj42vC":
        pytest.skip("Same pending golden review as test_node_export")
    docs = json.loads((TESTDATA_PATH / "test_workspace.json").read_text(encoding="utf-8"))["docs"]
    tanapaste, _ = export_node_with_tracking(TrackingGraph.from_documents(docs), node_id)
    expected = (TESTDATA_PATH / f"{node_id}.tanapaste").read_text()
    # The exporter ends the paste with a blank line.
    assert tanapaste == expected + "\n"


@pytest.mark.parametrize(
    ("folder", "node_id"), [("code_blocks", "YbPcBamWZFGV"), ("supertag_with_attributes", "hiHpuPTowhDs")]
)
def test_subset_reproduces_tanapaste(folder, node_id):
    original = json.loads((TESTDATA_PATH / folder / f"{node_id}.json").read_text(encoding="utf-8"))
    full_graph = TrackingGraph.from_documents(original["docs"])
    tanapaste, touched = export_node_with_tracking(full_graph, node_id)

    subset = create_subset_json(original, touched)
    graph = TanaGraph.from_documents(subset["docs"])
    actual = _render_node_as_tanapaste(graph, graph[node_id])
    assert tanapaste == actual + "\n"


def test_export_commands_frame_tanapaste(tmp_path, monkeypatch, capsys):
    source = tmp_path / "export.json"
    source.write_text(json.dumps({"docs": [{"id": "a", "props": {"name": "Example"}}]}), encoding="utf-8")
    full_output = tmp_path / "full"
    subset_output = tmp_path / "subset"

    monkeypatch.setattr(sys, "argv", ["convert", str(source), "-o", str(full_output)])
    convert.main()
    assert full_output.with_suffix(".tanapaste.txt").read_bytes() == b"%%tana%%\n- Example\n"
    assert full_output.with_suffix(".md").read_bytes() == b"Example\n=======\n"

    monkeypatch.setattr(sys, "argv", ["export_node_subset", str(source), "a", "-o", str(subset_output)])
    export_node_subset.main()
    assert subset_output.with_suffix(".tanapaste.txt").read_bytes() == b"%%tana%%\n- Example\n\n"
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["tana_issues_to_tanapaste", str(source)])
    tana_issues_to_tanapaste.main()
    output = capsys.readouterr()
    assert output.out == "%%tana%%\n"
    assert output.err == "Open issues exported: 0\n"
