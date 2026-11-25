"""The scroll proposal fragments resolve in the pinned editor's shared context."""

import hashlib
import json
import os
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles


def test_proposal_fragments(runfiles: Runfiles) -> None:
    def resolve(key: str) -> Path:
        path = runfiles.Rlocation(os.environ[key])
        assert path is not None
        return Path(path)

    canonical_by_chunk = {p["chunk_id"]: p for p in json.loads(resolve("CANONICAL").read_text())["placements"]}
    selected_by_chunk = {p["chunk_id"]: p for p in json.loads(resolve("SELECTED").read_text())["placements"]}
    assert set(canonical_by_chunk) == set(selected_by_chunk) == {"main", "editor"}
    canonical = canonical_by_chunk["editor"]
    selected = selected_by_chunk["editor"]
    (chunks,) = (f for f in os.environ["INPUTS"].split() if f.endswith("/chunks"))
    directory = runfiles.Rlocation(chunks)
    assert directory is not None
    digest = hashlib.sha256((Path(directory) / "editor.js").read_bytes()).hexdigest()
    assert selected["source_sha256"] == canonical["source_sha256"] == digest

    # One name already in the shared bounds file, one appended there, and
    # declarations in both new modules. These names come from Rust's solve.
    names = {name: unit["owner"] for unit in canonical["units"] for name in unit.get("members", {}).values()}
    assert names["getVisibleSceneBounds"] == names["getBezierValueForT"] == "packages/excalidraw/element/bounds"
    assert names["generateRoughOptions"] == "packages/excalidraw/scene/shape"
    assert names["getCurvePathOps"] == "packages/utils/geometry/shape"

    selected_owners = {name: unit["owner"] for unit in selected["units"] for name in unit.get("members", {}).values()}
    assert selected_owners["line"] == selected_owners["linesIntersectAt"] == "packages/math/line"
    assert selected_owners["triangleIncludesPoint"] == "packages/math/triangle"

    for full, retained in zip(canonical["units"], selected["units"], strict=True):
        assert full["ordinal"] == retained["ordinal"]
        if retained["owner"] is not None:
            assert retained["owner"] == full["owner"]
            assert retained.get("members", {}).items() <= full.get("members", {}).items()


if __name__ == "__main__":
    pytest_bazel.main()
