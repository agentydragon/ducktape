"""The committed binding CRDs and kubeconform schemas are what `generate` writes (STYLE.md § Testing,
generated-output snapshot): regenerate with `bb run //x/agentplane/crds:generate_bin` and commit the
result when this fails.
"""

from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path
from x.agentplane.crds.generate import CRD_FILES, CRDS_DIR, generated_files


def _committed(relative: Path) -> str:
    return get_required_path(f"_main/{relative}").read_text()


def test_committed_files_are_generated() -> None:
    for relative, content in generated_files({name: _committed(CRDS_DIR / name) for name in CRD_FILES}):
        assert content == _committed(relative), f"{relative} is stale"


if __name__ == "__main__":
    pytest_bazel.main()
