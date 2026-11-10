"""The residual baseline emits an accepted, complete tree for every input chunk."""

import json
import os
from pathlib import Path, PurePosixPath

import pytest
import pytest_bazel
from python.runfiles import Runfiles


@pytest.fixture(scope="module")
def out_dir(runfiles: Runfiles) -> Path:
    return Path(str(runfiles.Rlocation(os.environ["DEBUNDLE_OUT"])))


@pytest.fixture(scope="module")
def chunk_ids_by_file(runfiles: Runfiles) -> dict[str, str]:
    """Chunk ID by the bundle file name that imports name."""
    js_list = Path(str(runfiles.Rlocation(os.environ["DEBUNDLE_JS_LIST"])))
    # A line is `<file>.js`, or `<alias>.js<TAB><file>.js` loading `<file>.js` as chunk `<alias>`.
    lines = [line.split("\t") for line in js_list.read_text().splitlines()]
    return {fields[-1]: fields[0].removesuffix(".js") for fields in lines}


@pytest.fixture(scope="module")
def chunk_ids(chunk_ids_by_file: dict[str, str]) -> set[str]:
    return set(chunk_ids_by_file.values())


def test_gate_accepted(out_dir: Path) -> None:
    assert not list(out_dir.rglob("GATE_REJECTED"))


def test_every_chunk_accepted_and_translation_validated(out_dir: Path, chunk_ids: set[str]) -> None:
    reports = out_dir / "reports/tree"
    summaries = {
        path.parent.relative_to(reports).as_posix(): json.loads(path.read_text())
        for path in reports.rglob("graph_summary.json")
    }
    assert set(summaries) == chunk_ids
    rejected = {
        chunk
        for chunk, summary in summaries.items()
        if not (summary["gate_accepts"] and summary["translation_validation_accepts"])
    }
    assert rejected == set()


def test_every_unswapped_chunk_entry_emitted(out_dir: Path, chunk_ids: set[str]) -> None:
    app = out_dir / "app"
    emitted = {path.parent.relative_to(app).as_posix() for path in app.rglob("entry.js") if path.stat().st_size}
    swapped = {
        swap["chunk_id"] for swap in json.loads((out_dir / "reports/vendor_swaps.json").read_text())["full"].values()
    }
    assert emitted == chunk_ids - swapped


def test_static_imports_resolve_to_emitted_chunks(out_dir: Path, chunk_ids_by_file: dict[str, str]) -> None:
    unresolved = {
        (path.parent.name, chunk_import["source"])
        for path in (out_dir / "reports/tree").rglob("chunk.json")
        for chunk_import in json.loads(path.read_text())["imports"]
        if PurePosixPath(chunk_import["source"]).name not in chunk_ids_by_file
    }
    assert unresolved == set()


if __name__ == "__main__":
    pytest_bazel.main()
