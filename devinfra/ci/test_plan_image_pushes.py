import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.bes import BuildBuddyError, Invocation, Output, merge
from devinfra.ci.plan_image_pushes import (
    DEVEL_TAG_RE,
    REGISTRY_PREFIX,
    Crane,
    Decision,
    Image,
    NotPublishedError,
    decide,
    digest_uri,
    load_images,
    local_digests,
    matrix_include,
    plan,
)

BIN = "bazel-out/k8-fastbuild/bin"


def image(name: str = "airlock", target: str = "//airlock:image", test: str | None = None) -> Image:
    return Image(name=name, target=target, test=test, registry="ghcr")


def digest_output(label: str, path: str, uri: str = "bytestream://h/blobs/x/72") -> Output:
    return Output(label=label, path=path, uri=uri, digest="ignored", size=72, output_group="default")


def invocation(*outputs: Output) -> Invocation:
    return Invocation(outputs=list(outputs), test_status={})


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


def test_the_digest_target_is_the_image_label_plus_suffix() -> None:
    assert image().digest_label == "//airlock:image.digest"
    assert image(target="@ducktape_manifold_mcp_server//:image").digest_label == (
        "@ducktape_manifold_mcp_server//:image.digest"
    )


def test_outputs_are_found_by_label_not_by_guessing_a_path() -> None:
    """Most images are literally named `image`, and an external repo's directory is
    mangled by bzlmod — a path guess resolves to another image's file, silently."""
    inv = invocation(
        digest_output("//airlock:image.digest", f"{BIN}/airlock/image.json.sha256", "bytestream://h/blobs/air/72"),
        digest_output(
            "//props/backend:image.digest", f"{BIN}/props/backend/image.json.sha256", "bytestream://h/b/p/72"
        ),
    )
    by_label = inv.by_label()
    assert digest_uri(image(), by_label) == "bytestream://h/blobs/air/72"
    assert digest_uri(image(name="props-backend", target="//props/backend:image"), by_label) == "bytestream://h/b/p/72"


def test_an_image_the_build_never_produced_has_no_uri() -> None:
    """`//...` does not reach an external repository, so manifold-mcp-server is
    absent from every sweep and must take the slow path."""
    assert digest_uri(image(target="@repo//:image"), invocation().by_label()) is None


def test_the_same_digest_file_reported_twice_is_one_file() -> None:
    """bazel-ci reports two invocations and both name every non-test target."""
    same = digest_output("//airlock:image.digest", f"{BIN}/airlock/image.json.sha256")
    inv = merge([invocation(same), invocation(same)])
    assert inv is not None
    assert digest_uri(image(), inv.by_label()) == same.uri


def test_two_different_digest_files_for_one_label_pushes_that_image() -> None:
    """Which file is the digest is unknowable, so this image cannot be skipped.

    It must not decide anything about the others: the planner keeps going.
    """
    inv = invocation(
        digest_output("//airlock:image.digest", f"{BIN}/airlock/image.json.sha256"),
        digest_output("//airlock:image.digest", f"{BIN}/other/image.json.sha256"),
    )
    assert digest_uri(image(), inv.by_label()) is None


def test_the_digest_is_the_file_contents_not_the_file_digest() -> None:
    """A release's identity is its output's digest; an image's identity is inside
    the file, so the bytes have to be fetched."""
    inv = invocation(digest_output("//airlock:image.digest", f"{BIN}/airlock/image.json.sha256"))
    got = local_digests([image()], inv, lambda _: b"sha256:deadbeef\n")
    assert got == {"airlock": "sha256:deadbeef"}


def test_an_unreadable_blob_keeps_the_image(capsys: pytest.CaptureFixture[str]) -> None:
    def explode(_: str) -> bytes:
        raise BuildBuddyError("CAS unreachable")

    inv = invocation(digest_output("//airlock:image.digest", f"{BIN}/airlock/image.json.sha256"))
    assert local_digests([image()], inv, explode) == {}
    assert "could not read its digest" in capsys.readouterr().err


def test_no_invocation_means_no_digests_and_so_everything_pushes() -> None:
    """push-images runs under always(); a skipped bazel-ci must publish too much."""
    images = [image(name=n, target=f"//{n}:image") for n in ("a", "b", "c")]
    assert local_digests(images, None, lambda _: b"") == {}
    decisions = plan(images, lambda i: decide(i, {}, FakeCrane({})), workers=3)
    assert len(matrix_include(decisions)) == len(images)


def test_unchanged_digest_is_not_pushed() -> None:
    subject = image(test="//airlock/...")
    crane = FakeCrane({subject.repo: {"devel-20260826120000-abc1234": "sha256:same"}})
    decision = decide(subject, {"airlock": "sha256:same"}, crane)
    assert not decision.needs_push
    assert matrix_include([decision]) == []


def test_changed_digest_is_pushed() -> None:
    subject = image(test="//airlock/...")
    crane = FakeCrane({subject.repo: {"devel-20260826120000-abc1234": "sha256:old"}})
    decision = decide(subject, {"airlock": "sha256:new"}, crane)
    assert decision.needs_push
    assert matrix_include([decision]) == [
        {"image_name": "airlock", "image": "//airlock:image", "test_target": "//airlock/...", "registry": "ghcr"}
    ]


def test_never_published_image_is_pushed() -> None:
    decision = decide(image(), {"airlock": "sha256:new"}, FakeCrane({}))
    assert decision.needs_push
    assert decision.published_tag is None
    assert matrix_include([decision])[0]["test_target"] == ""


def test_only_devel_shaped_tags_decide_the_comparison() -> None:
    """`latest` and hand-made tags must not stand in for the tag Flux tracks."""
    subject = image()
    crane = FakeCrane({subject.repo: {"latest": "sha256:new", "scratch": "sha256:new"}})
    assert decide(subject, {"airlock": "sha256:new"}, crane).needs_push


def test_newest_devel_tag_wins() -> None:
    subject = image()
    crane = FakeCrane(
        {
            subject.repo: {
                "devel-20260101000000-aaaaaaa": "sha256:ancient",
                "devel-20260826120000-bbbbbbb": "sha256:newest",
            }
        }
    )
    assert not decide(subject, {"airlock": "sha256:newest"}, crane).needs_push


def test_a_registry_error_fails_the_plan_rather_than_skipping() -> None:
    """An unreadable registry is not evidence the image is unchanged."""

    def explode(_: Image) -> Decision:
        raise RuntimeError("crane ls failed (exit 1):\nunexpected status code 500")

    with pytest.raises(RuntimeError, match="500"):
        plan([image()], explode, workers=2)


def test_repo_url_follows_the_registry() -> None:
    forgejo = Image(name="osm-mcp", target="//third_party/osmmcp:image", test=None, registry="forgejo")
    assert image().repo.startswith(REGISTRY_PREFIX["ghcr"])
    assert forgejo.repo.startswith(REGISTRY_PREFIX["forgejo"])
    assert image().repo.endswith("/airlock")


def test_load_images_rejects_an_unknown_registry(tmp_path: Path) -> None:
    spec = tmp_path / "image_targets.json"
    spec.write_text(json.dumps({"images": {"a": {"target": "//a:image", "registry": "dockerhub"}}}))
    with pytest.raises(ValueError, match="unknown registry"):
        load_images(spec)


def test_every_checked_in_image_declares_a_usable_target() -> None:
    """An invariant over the roster rather than a copy of it: adding an image
    passes, a malformed label or unknown registry fails."""
    images = load_images(Path("devinfra/ci/image_targets.json"))
    assert images, "image_targets.json declares no images"
    for subject in images:
        assert subject.digest_label.endswith(".digest")
        assert "//" in subject.target, subject.name
        assert subject.repo.endswith(f"/{subject.name}")


def test_absent_repo_is_reported_as_not_published() -> None:
    """crane's NAME_UNKNOWN is a defined absent state; other failures must raise."""

    class Stub(Crane):
        def __init__(self, stderr: str) -> None:
            super().__init__("crane")
            self.stderr = stderr

        def _run(self, *args: str) -> str:
            if any(m in self.stderr for m in ("NAME_UNKNOWN", "MANIFEST_UNKNOWN")):
                raise NotPublishedError(self.stderr)
            raise RuntimeError(self.stderr)

    assert Stub("NAME_UNKNOWN: repository name not known").latest_devel_tag("r") is None
    with pytest.raises(RuntimeError):
        Stub("unexpected status code 500").latest_devel_tag("r")


if __name__ == "__main__":
    pytest_bazel.main()
