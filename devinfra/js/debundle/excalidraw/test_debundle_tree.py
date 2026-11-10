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
def chunk_ids(runfiles: Runfiles) -> set[str]:
    js_list = Path(str(runfiles.Rlocation(os.environ["DEBUNDLE_JS_LIST"])))
    return {line.removesuffix(".js") for line in js_list.read_text().splitlines()}


def test_gate_accepted(out_dir: Path) -> None:
    assert not list(out_dir.rglob("GATE_REJECTED"))


def test_every_chunk_accepted_and_translation_validated(out_dir: Path, chunk_ids: set[str]) -> None:
    summaries = {
        path.parent.name: json.loads(path.read_text())
        for path in (out_dir / "reports/tree").glob("*/graph_summary.json")
    }
    assert set(summaries) == chunk_ids
    rejected = {
        chunk
        for chunk, summary in summaries.items()
        if not (summary["gate_accepts"] and summary["translation_validation_accepts"])
    }
    assert rejected == set()


def test_every_chunk_entry_emitted(out_dir: Path, chunk_ids: set[str]) -> None:
    emitted = {path.parent.name for path in (out_dir / "app").glob("*/entry.js") if path.stat().st_size}
    assert emitted == chunk_ids


def test_static_imports_resolve_to_emitted_chunks(out_dir: Path, chunk_ids: set[str]) -> None:
    unresolved = set()
    for path in (out_dir / "reports/tree").glob("*/chunk.json"):
        for chunk_import in json.loads(path.read_text())["imports"]:
            target = PurePosixPath(chunk_import["source"])
            if target.suffix != ".js" or target.stem not in chunk_ids:
                unresolved.add((path.parent.name, chunk_import["source"]))
    assert unresolved == set()


if __name__ == "__main__":
    pytest_bazel.main()
