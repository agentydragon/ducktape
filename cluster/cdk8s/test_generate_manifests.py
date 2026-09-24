"""Pinning tests for the cdk8s manifest generators.

The generated-output snapshot (STYLE.md § Testing) over every file the generator writes:
the committed files are the source of truth, and regeneration must reproduce them exactly.
`GENERATED_ROOT` is closed in the other direction too, so a hand-added or stale file there
fails; generated files beside hand-written ones under `HAND_WRITTEN_ROOT` are pinned only
one way.
"""

from pathlib import Path

import pytest
import pytest_bazel

from cluster.cdk8s.generate_manifests import generate_manifests
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from util.bazel.runfiles import get_required_path

_REGENERATE = "regenerate with `bb run //cluster/cdk8s:generate_manifests` and commit the result"


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("generated")
    generate_manifests(root)
    return root


@pytest.fixture(scope="module")
def checkout() -> Path:
    """The runfiles tree, which holds each committed file the data deps package at its repo path."""
    return get_required_path(f"_main/{GENERATED_ROOT}").parents[1]


def test_every_generated_file_is_committed(generated: Path, checkout: Path) -> None:
    missing = sorted(relative for relative in _files(generated) if not (checkout / relative).is_file())
    assert not missing, f"Generated but not committed ({_REGENERATE}):\n" + "\n".join(missing)


def test_generated_files_match_committed(generated: Path, checkout: Path) -> None:
    stale = sorted(
        relative
        for relative in _files(generated)
        if (checkout / relative).is_file() and (generated / relative).read_text() != (checkout / relative).read_text()
    )
    assert not stale, f"Stale ({_REGENERATE}):\n" + "\n".join(stale)


def test_generated_root_holds_only_generated_files(generated: Path, checkout: Path) -> None:
    committed = {f"{GENERATED_ROOT}/{relative}" for relative in _files(checkout / GENERATED_ROOT)}
    extra = sorted(committed - _files(generated))
    assert not extra, (
        f"Committed under {GENERATED_ROOT} but not written by the generator; delete it, or keep a "
        "directory holding a hand-written file whole under the hand-written root:\n" + "\n".join(extra)
    )


def test_no_image_automation_markers(generated: Path) -> None:
    # cdk8s can't emit YAML comments, so this should be unreachable -- but if it ever did,
    # Flux's image-automation bot would silently fight the generator for ownership of the
    # file (cluster/docs/cdk8s.md).
    marked = sorted(relative for relative in _files(generated) if "$imagepolicy" in (generated / relative).read_text())
    assert not marked, "Generated files must not carry a Flux image-automation marker:\n" + "\n".join(marked)


if __name__ == "__main__":
    pytest_bazel.main()
