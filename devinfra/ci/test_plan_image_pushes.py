import json
from pathlib import Path

import pytest

from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, Output, merge
from devinfra.ci.image_registry import Registry
from devinfra.ci.plan_image_pushes import Image, decide, digest_uri, load_images, local_digests, main, matrix_include
from devinfra.ci.publication_test_verdicts import TestEvidence
from devinfra.oci.crane import Crane, CraneError

NO_CI = TestEvidence(problem="no CI invocation")

BIN = "bazel-out/k8-fastbuild/bin"


def image(name: str = "airlock", target: str = "//airlock:image", test: str | None = None) -> Image:
    return Image(name=name, target=target, test=test, registry=Registry.GHCR)


def digest_output(label: str, path: str, uri: str = "bytestream://h/blobs/x/72") -> Output:
    return Output(label=label, path=path, uri=uri, digest="ignored", output_group="default")


def invocation(*outputs: Output) -> Invocation:
    return Invocation(outputs=list(outputs))


class FakeCrane(Crane):
    """Registry stub: repo -> {tag: digest}. A repo with no `latest` was never pushed.

    A real `Crane` with the subprocess replaced, as `devinfra/oci/test_crane.py` does — the
    binary is never resolved because nothing reaches `_run`.
    """

    def __init__(self, repos: dict[str, dict[str, str]]) -> None:
        super().__init__(Path("/nonexistent/crane"))
        self.repos = repos

    def digest_or_none(self, image_ref: str) -> str | None:
        repo, _, tag = image_ref.rpartition(":")
        return self.repos.get(repo, {}).get(tag)


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
    """`//...` does not reach an external repository, so its digest is absent from
    every sweep and must take the slow path."""
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


def test_missing_build_evidence_defers_to_push_jobs_without_contacting_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / "images.json"
    spec.write_text(json.dumps({"images": {"a": {"target": "//a:image"}, "b": {"target": "//b:image"}}}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    main(["--spec", str(spec), "--crane", str(tmp_path / "nonexistent")])

    matrix, count = output.read_text().splitlines()
    assert count == "count=2"
    assert [row["image_name"] for row in json.loads(matrix.removeprefix("matrix="))["include"]] == ["a", "b"]


def test_unchanged_digest_is_not_pushed() -> None:
    subject = image(test="//airlock/...")
    crane = FakeCrane({subject.repo: {"latest": "sha256:same"}})
    decision = decide(subject, {"airlock": "sha256:same"}, crane)
    assert not decision.needs_push
    assert matrix_include([decision], NO_CI) == []


def test_changed_digest_is_pushed() -> None:
    subject = image(test="//airlock/...")
    crane = FakeCrane({subject.repo: {"latest": "sha256:old"}})
    decision = decide(subject, {"airlock": "sha256:new"}, crane)
    assert decision.needs_push
    assert matrix_include([decision], NO_CI) == [
        {"image_name": "airlock", "image": "//airlock:image", "test_target": "//airlock/...", "registry": "ghcr"}
    ]


def test_an_image_whose_gate_push_ci_already_failed_gets_no_job() -> None:
    decision = decide(image(test="//airlock:test"), {"airlock": "sha256:new"}, FakeCrane({}))
    assert decision.needs_push
    assert matrix_include([decision], TestEvidence(problem=None, failed_labels=frozenset({"//airlock:test"}))) == []


def test_the_matrix_serializes_the_registry_as_the_name_the_workflow_compares() -> None:
    """`push-images.yml` gates its credential step on `matrix.registry == 'forgejo'`,
    so the enum must reach GitHub as its bare value, not its member name."""
    forgejo = Image(name="example", target="//example:image", test=None, registry=Registry.FORGEJO)
    decision = decide(forgejo, {"example": "sha256:new"}, FakeCrane({}))
    assert json.loads(json.dumps(matrix_include([decision], NO_CI)))[0]["registry"] == "forgejo"


def test_never_published_image_is_pushed() -> None:
    decision = decide(image(), {"airlock": "sha256:new"}, FakeCrane({}))
    assert decision.needs_push
    assert decision.published_digest is None
    assert matrix_include([decision], NO_CI)[0]["test_target"] == ""


def test_content_matching_an_older_publish_is_still_pushed() -> None:
    """Only the most recent publish counts. An image whose content reverts to what
    some earlier tag holds is a change from what is deployed, so it must go out."""
    subject = image()
    crane = FakeCrane({subject.repo: {"latest": "sha256:current", "devel-20260101000000-aaaaaaa": "sha256:reverted"}})
    assert decide(subject, {"airlock": "sha256:reverted"}, crane).needs_push


def test_registry_access_failure_stops_the_planner_before_it_writes_any_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / "images.json"
    spec.write_text(json.dumps({"images": {"pending": {"target": "//p:image"}, "known": {"target": "//k:image"}}}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("devinfra.ci.plan_image_pushes.local_digests", lambda *_: {"known": "sha256:current"})
    crane = tmp_path / "crane"
    crane.write_text("#!/bin/sh\necho 'DENIED: requested access is denied' >&2\nexit 1\n")
    crane.chmod(0o755)

    with pytest.raises(CraneError, match="DENIED") as error:
        main(["--spec", str(spec), "--crane", str(crane)])

    assert error.value.command == ("digest", "ghcr.io/agentydragon/known:latest")
    assert not output.exists()


def test_load_images_rejects_a_field_it_does_not_understand(tmp_path: Path) -> None:
    """A misspelt key used to be ignored, which dropped an image's test gate or sent
    it to the wrong registry — silently, and only visible once it had published."""
    spec = tmp_path / "image_targets.json"
    spec.write_text(json.dumps({"images": {"a": {"target": "//a:image", "tests": "//a:test"}}}))
    with pytest.raises(ValueError, match="unknown field"):
        load_images(spec)


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
        assert "//" in subject.target, subject.name
