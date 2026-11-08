from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from devinfra.ci.nix_images import Image, changed_images, load_images, marker_path


def test_workflow_triggers_on_exactly_the_image_inputs() -> None:
    workflow = yaml.load(
        (Runfiles.CreateOrRaise().root() / "ducktape/.github/workflows/nix-images.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    inputs = {p for image in load_images().values() for p in image.paths}
    for event in ("push", "pull_request", "pull_request_target"):
        assert set(workflow["on"][event]["paths"]) == inputs, event


def test_smoke_scripts_are_image_inputs() -> None:
    for image in load_images().values():
        if image.smoke is not None:
            assert image.smoke in image.paths or any(
                p.endswith("/**") and image.smoke.startswith(p.removesuffix("**")) for p in image.paths
            ), image.attr


def test_marker_path_matches_nix_to_file() -> None:
    # From `nix eval --raw --expr 'builtins.toFile "nix-image-verified" "verified website-image
    # /nix/store/abc-website\n"'` (Nix 2.31).
    assert marker_path("website-image", "/nix/store/abc-website") == (
        "/nix/store/fblrhqlxxgqkym4dhs44izbdvqvzxhk6-nix-image-verified"
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)

    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True)

    git("init", "--quiet")
    (tmp_path / "shared.nix").write_text("a")
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("a")
    git("add", ".")
    git("commit", "--quiet", "-m", "base")
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
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "change"],
        check=True,
    )
    assert changed_images(IMAGES, "HEAD^1", "HEAD") == expected


def test_no_base_selects_every_image() -> None:
    assert changed_images(IMAGES, None, "HEAD") == ["shared-image", "site-image"]


if __name__ == "__main__":
    pytest_bazel.main()
