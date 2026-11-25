from pathlib import Path

import pytest_bazel

from devinfra.lint.x.py_detectors.import_graph import build_import_graph


def test_graph_only_contains_project_sources(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import json")
    deps = tmp_path / "node_modules"
    deps.mkdir()
    (deps / "helper.py").write_text("import app")
    assert build_import_graph(tmp_path) == {"app": {"json"}}


if __name__ == "__main__":
    pytest_bazel.main()
