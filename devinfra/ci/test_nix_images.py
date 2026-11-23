from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from devinfra.ci import nix_images
from devinfra.ci.nix_images import BUILD_JOB, Image, changed_images, load_images
from testing.git_repo import git, init_git_repo


def test_workflow_matches_roster_and_wait_build() -> None:
    workflow = yaml.load(
        (Runfiles.CreateOrRaise().root() / "ducktape/.github/workflows/nix-images.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    inputs = {p for image in load_images().values() for p in image.paths}
    for event in ("push", "pull_request", "pull_request_target"):
        assert set(workflow["on"][event]["paths"]) == inputs, event
    assert workflow["jobs"]["build"]["name"] == BUILD_JOB


def test_smoke_scripts_are_image_inputs() -> None:
    for image in load_images().values():
        if image.smoke is not None:
            assert image.smoke in image.paths or any(
                p.endswith("/**") and image.smoke.startswith(p.removesuffix("**")) for p in image.paths
            ), image.attr


def test_build_only_marks_images_that_pass_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    images = {attr: Image(attr=attr, repository=attr, paths=(), smoke=f"{attr}.sh") for attr in ("working", "broken")}
    for attr, status in [("working", 0), ("broken", 1)]:
        (tmp_path / f"{attr}.sh").write_text(f"exit {status}\n")
    outs = {attr: f"/nix/store/{attr}" for attr in images}
    monkeypatch.setattr(nix_images, "out_paths", lambda attrs: outs)
    monkeypatch.setattr(nix_images, "in_cache", lambda marker: False)
    # Model the Nix store at its CLI boundary. build and path-info succeed for both
    # images; the actual smoke scripts decide which marker reaches the store.
    nix = tmp_path / "nix"
    nix.write_text(
        f"#!{sys.executable}\n"
        """import sys
from pathlib import Path
if sys.argv[1] == "--eval":
    content = sys.argv[sys.argv.index("--argstr") + 2]
    if "--read-write-mode" in sys.argv:
        with Path("markers").open("a") as markers:
            markers.write(content)
    print("/nix/store/marker", end="")
"""
    )
    nix.chmod(0o755)
    (tmp_path / "nix-instantiate").symlink_to(nix)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)

    with pytest.raises(RuntimeError, match="broken: smoke test"):
        nix_images.build(images, list(images))

    assert (tmp_path / "markers").read_text() == "verified working /nix/store/working\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)

    init_git_repo(tmp_path)
    (tmp_path / "shared.nix").write_text("a")
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("a")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "base")
    return tmp_path


IMAGES = {
    "site-image": Image(attr="site-image", repository="registry.invalid/site", paths=("site/**",), smoke=None),
    "shared-image": Image(attr="shared-image", repository="registry.invalid/shared", paths=("shared.nix",), smoke=None),
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [("site/deep/page.html", ["site-image"]), ("shared.nix", ["shared-image"]), ("unrelated.txt", [])],
)
def test_changed_images_matches_globs(repo: Path, path: str, expected: list[str]) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("b")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "change")
    assert changed_images(IMAGES, "HEAD^1", "HEAD") == expected


def test_no_base_selects_every_image() -> None:
    assert changed_images(IMAGES, None, "HEAD") == ["shared-image", "site-image"]


if __name__ == "__main__":
    pytest_bazel.main()
