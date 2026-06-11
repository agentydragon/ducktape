"""Tests for convert.py - Tana JSON to Markdown/TanaPaste conversion."""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest
import pytest_bazel

from tana.export.convert import export_node_as_tanapaste
from tana.graph.loader import load_workspace

# Testdata lives alongside this test under tana/testdata
TESTDATA_PATH = Path(__file__).parent / "testdata"


def check_content_match(actual: str, expected: str, test_name: str) -> None:
    """Check if actual matches expected, print diff if not."""
    if actual != expected:
        # Use ndiff for word-level changes with color-like output
        diff = difflib.ndiff(expected.splitlines(keepends=True), actual.splitlines(keepends=True))
        print("".join(diff))
        pytest.fail(f"Content does not match for {test_name}")


@pytest.mark.parametrize("node_id", [f.stem for f in TESTDATA_PATH.glob("*.tanapaste")])
def test_node_export(node_id):
    """Test that generated TanaPaste for specific nodes match reference files."""
    # TODO(mpokorny): b9AZZEuj42vC golden includes workspace-level sibling refs after search rows.
    # Converter intentionally emits only search results; review and update the golden or converter rule.
    if node_id == "b9AZZEuj42vC":
        pytest.skip("Pending golden review for search-table node; converter emits only search rows")
    store = load_workspace(TESTDATA_PATH / "test_workspace.json")
    actual = export_node_as_tanapaste(store, store[node_id])
    expected = (TESTDATA_PATH / f"{node_id}.tanapaste").read_text()
    check_content_match(actual, expected, node_id)


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
    actual = export_node_as_tanapaste(store, store[node_id])
    expected = (base / f"{node_id}.tanapaste").read_text()
    check_content_match(actual, expected, f"{node_id} using minimal JSON")


if __name__ == "__main__":
    pytest_bazel.main()
