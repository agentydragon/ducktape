import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.plan_image_pushes import (
    DEVEL_TAG_RE,
    REGISTRY_PREFIX,
    Crane,
    Decision,
    Image,
    NotPublishedError,
    decide,
    digest_glob,
    digest_target,
    load_images,
    matrix_include,
    plan,
    resolve_digest_file,
)

BIN = "bb-out/bazel-out/k8-fastbuild/bin"


def write_spec(tmp_path: Path, images: dict) -> Path:
    spec = tmp_path / "image_targets.json"
    spec.write_text(json.dumps({"images": images}))
    return spec


def write_digest(root: Path, relative: str, digest: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(digest + "\n")
    return path


class FakeCrane:
    """Registry stub: repo -> {tag: digest}. A missing repo has never been pushed."""

    def __init__(self, repos: dict[str, dict[str, str]]) -> None:
        self.repos = repos

    def latest_devel_tag(self, repo: str) -> str | None:
        tags = self.repos.get(repo)
        if tags is None:
            return None
        return max((t for t in tags if DEVEL_TAG_RE.match(t)), default=None)

    def digest(self, ref: str) -> str | None:
        repo, _, tag = ref.rpartition(":")
        return self.repos.get(repo, {}).get(tag)


def test_digest_target_is_the_sibling_label() -> None:
    assert digest_target("//airlock:image") == "//airlock:image.digest"


def test_digest_glob_resolves_main_repo_label_exactly() -> None:
    assert digest_glob("//airlock:image") == f"{BIN}/airlock/image.json.sha256"
    assert digest_glob("//x/authentik_mcp_poc:server_image") == f"{BIN}/x/authentik_mcp_poc/server_image.json.sha256"


def test_digest_glob_wildcards_only_the_canonical_repo_dir() -> None:
    """bzlmod mangles an external repo's directory name, so it can't be derived."""
    assert digest_glob("@ducktape_manifold_mcp_server//:image") == f"{BIN}/external/*/image.json.sha256"


def test_digest_glob_rejects_a_label_without_an_explicit_target() -> None:
    with pytest.raises(ValueError, match="must name its target"):
        digest_glob("//airlock")

    with pytest.raises(ValueError, match="not a Bazel label"):
        digest_glob("airlock:image")


def test_same_target_name_in_different_packages_stays_distinct() -> None:
    """Many images are literally named `image`; a name-only search would collide."""
    assert digest_glob("//airlock:image") != digest_glob("//props/backend:image")


def test_a_digest_that_was_not_downloaded_resolves_to_none(tmp_path: Path) -> None:
    """`bb remote build` does not reliably return every output; that is not a fault."""
    assert resolve_digest_file("//airlock:image", tmp_path) is None


def test_two_matching_digest_files_is_still_a_bug(tmp_path: Path) -> None:
    write_digest(tmp_path, f"{BIN}/external/a/image.json.sha256", "sha256:a")
    write_digest(tmp_path, f"{BIN}/external/b/image.json.sha256", "sha256:b")
    with pytest.raises(RuntimeError, match="expected at most one"):
        resolve_digest_file("@some_repo//:image", tmp_path)


def test_an_undownloaded_digest_keeps_the_image(tmp_path: Path) -> None:
    """Fail open. Hard-failing here broke image publishing on devel entirely."""
    image = Image(name="airlock", target="//airlock:image", test=None, registry="ghcr")
    crane = FakeCrane({image.repo: {"devel-20260826120000-abc1234": "sha256:whatever"}})

    decision = decide(image, tmp_path, crane)
    assert decision.local_digest is None
    assert decision.needs_push
    assert [row["image_name"] for row in matrix_include([decision])] == ["airlock"]


def test_a_whole_failed_download_degrades_to_the_old_fan_out(tmp_path: Path) -> None:
    """Worst case is every image kept, never a silently dropped push."""
    images = [Image(name=n, target=f"//{n}:image", test=None, registry="ghcr") for n in ("a", "b", "c")]
    decisions = plan(images, lambda i: decide(i, tmp_path, FakeCrane({})), workers=3)
    assert len(matrix_include(decisions)) == len(images)


def test_repo_url_follows_the_registry() -> None:
    ghcr = Image(name="airlock", target="//airlock:image", test=None, registry="ghcr")
    forgejo = Image(name="osm-mcp", target="//third_party/osmmcp:image", test=None, registry="forgejo")
    assert ghcr.repo.startswith(REGISTRY_PREFIX["ghcr"])
    assert forgejo.repo.startswith(REGISTRY_PREFIX["forgejo"])
    assert ghcr.repo.endswith("/airlock")


def test_load_images_defaults_registry_and_absent_test(tmp_path: Path) -> None:
    spec = write_spec(tmp_path, {"a": {"target": "//a:image"}, "b": {"target": "//b:image", "registry": "forgejo"}})
    by_name = {i.name: i for i in load_images(spec)}
    assert by_name["a"].registry == "ghcr"
    assert by_name["a"].test is None
    assert by_name["b"].registry == "forgejo"


def test_load_images_rejects_an_unknown_registry(tmp_path: Path) -> None:
    spec = write_spec(tmp_path, {"a": {"target": "//a:image", "registry": "dockerhub"}})
    with pytest.raises(ValueError, match="unknown registry"):
        load_images(spec)


def test_unchanged_digest_is_not_pushed(tmp_path: Path) -> None:
    write_digest(tmp_path, f"{BIN}/airlock/image.json.sha256", "sha256:same")
    image = Image(name="airlock", target="//airlock:image", test="//airlock/...", registry="ghcr")
    crane = FakeCrane({image.repo: {"devel-20260826120000-abc1234": "sha256:same"}})

    decision = decide(image, tmp_path, crane)
    assert not decision.needs_push
    assert matrix_include([decision]) == []


def test_changed_digest_is_pushed(tmp_path: Path) -> None:
    write_digest(tmp_path, f"{BIN}/airlock/image.json.sha256", "sha256:new")
    image = Image(name="airlock", target="//airlock:image", test="//airlock/...", registry="ghcr")
    crane = FakeCrane({image.repo: {"devel-20260826120000-abc1234": "sha256:old"}})

    decision = decide(image, tmp_path, crane)
    assert decision.needs_push
    assert matrix_include([decision]) == [
        {"image_name": "airlock", "image": "//airlock:image", "test_target": "//airlock/...", "registry": "ghcr"}
    ]


def test_never_published_image_is_pushed(tmp_path: Path) -> None:
    write_digest(tmp_path, f"{BIN}/airlock/image.json.sha256", "sha256:new")
    image = Image(name="airlock", target="//airlock:image", test=None, registry="ghcr")

    decision = decide(image, tmp_path, FakeCrane({}))
    assert decision.needs_push
    assert decision.published_tag is None
    assert matrix_include([decision])[0]["test_target"] == ""


def test_only_devel_shaped_tags_decide_the_comparison(tmp_path: Path) -> None:
    """`latest` and hand-made tags must not stand in for the tag Flux tracks."""
    write_digest(tmp_path, f"{BIN}/airlock/image.json.sha256", "sha256:new")
    image = Image(name="airlock", target="//airlock:image", test=None, registry="ghcr")
    crane = FakeCrane({image.repo: {"latest": "sha256:new", "scratch": "sha256:new"}})

    assert decide(image, tmp_path, crane).needs_push


def test_newest_devel_tag_wins(tmp_path: Path) -> None:
    write_digest(tmp_path, f"{BIN}/airlock/image.json.sha256", "sha256:newest")
    image = Image(name="airlock", target="//airlock:image", test=None, registry="ghcr")
    crane = FakeCrane(
        {
            image.repo: {
                "devel-20260101000000-aaaaaaa": "sha256:ancient",
                "devel-20260826120000-bbbbbbb": "sha256:newest",
            }
        }
    )

    assert not decide(image, tmp_path, crane).needs_push


def test_a_registry_error_fails_the_plan_rather_than_skipping(tmp_path: Path) -> None:
    """An unreadable registry is not evidence the image is unchanged."""
    write_digest(tmp_path, f"{BIN}/airlock/image.json.sha256", "sha256:new")
    image = Image(name="airlock", target="//airlock:image", test=None, registry="ghcr")

    def explode(_: Image) -> Decision:
        raise RuntimeError("crane ls failed (exit 1):\nunexpected status code 500")

    with pytest.raises(RuntimeError, match="500"):
        plan([image], explode, workers=2)


def test_plan_decides_every_image(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        write_digest(tmp_path, f"{BIN}/{name}/image.json.sha256", f"sha256:{name}")
    images = [Image(name=n, target=f"//{n}:image", test=None, registry="ghcr") for n in ("a", "b", "c")]
    crane = FakeCrane({images[0].repo: {"devel-20260826120000-abc1234": "sha256:a"}})

    decisions = plan(images, lambda i: decide(i, tmp_path, crane), workers=3)
    assert {d.image.name for d in decisions if d.needs_push} == {"b", "c"}


def test_every_checked_in_image_is_addressable() -> None:
    """The real SSOT must parse and every label must resolve to a derivable digest path.

    An invariant over the roster rather than a copy of it: adding an image passes, a
    malformed label or unknown registry fails.
    """
    images = load_images(Path(__file__).parent / "image_targets.json")
    assert images, "image_targets.json declares no images"
    for image in images:
        assert digest_glob(image.target).endswith(".json.sha256")
        assert digest_target(image.target).endswith(".digest")
        assert image.repo.endswith(f"/{image.name}")


def test_absent_repo_is_reported_as_not_published() -> None:
    """crane's NAME_UNKNOWN is a defined absent state; other failures must raise."""

    class Stub(Crane):
        def __init__(self, stderr: str, returncode: int) -> None:
            super().__init__("crane")
            self.stderr, self.returncode = stderr, returncode

        def _run(self, *args: str) -> str:
            if any(m in self.stderr for m in ("NAME_UNKNOWN", "MANIFEST_UNKNOWN")):
                raise NotPublishedError(self.stderr)
            raise RuntimeError(self.stderr)

    assert Stub("NAME_UNKNOWN: repository name not known", 1).latest_devel_tag("r") is None
    with pytest.raises(RuntimeError):
        Stub("unexpected status code 500", 1).latest_devel_tag("r")


if __name__ == "__main__":
    pytest_bazel.main()
