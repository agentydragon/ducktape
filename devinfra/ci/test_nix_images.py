from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from devinfra.ci import nix_images
from devinfra.ci.nix_images import BUILD_JOB, Image, changed_images, load_images, marker_path


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


@pytest.mark.parametrize(
    "stderr",
    [
        "FATA[0000] Error listing repository tags: fetching tags list: name unknown: "
        "Repository name not known to registry",
        "FATA[0000] listing repository tags: fetching tags list: name unknown: Repository name not known to registry",
        'time="2026-11-10T00:00:00Z" level=fatal msg="listing repository tags: fetching tags list: name unknown: "',
    ],
)
def test_already_published_treats_unknown_repository_as_unpublished(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    def missing_repository(*args: str) -> dict[str, object]:
        raise subprocess.CalledProcessError(1, ["skopeo", "list-tags", *args], stderr=stderr)

    monkeypatch.setattr(nix_images, "_skopeo_json", missing_repository)
    assert nix_images.already_published((None, "sha256:local"), "registry.invalid/new-image") is False


@pytest.mark.parametrize(
    "stderr",
    [
        "FATA[0000] Error listing repository tags: Requesting bearer token: invalid status code 401 (Unauthorized)",
        "FATA[0000] Error listing repository tags: pinging registry: dial tcp: connection refused",
    ],
)
def test_already_published_propagates_auth_and_network_errors(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    def listing_failed(*args: str) -> dict[str, object]:
        raise subprocess.CalledProcessError(1, ["skopeo", "list-tags", *args], stderr=stderr)

    monkeypatch.setattr(nix_images, "_skopeo_json", listing_failed)
    with pytest.raises(subprocess.CalledProcessError):
        nix_images.already_published((None, "sha256:local"), "registry.invalid/new-image")


if __name__ == "__main__":
    pytest_bazel.main()
