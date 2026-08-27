import json
import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.bes import Invocation, Output
from devinfra.ci.plan_releases import (
    Release,
    bes_path,
    content_hash,
    content_tag,
    digests_from,
    is_published,
    load_releases,
    main,
    matrix_include,
    plan,
)
from devinfra.ci.release_content_hash import release_content_hash_from_digests

# Relative to the runfiles root, which is cwd under `bazel test` — the same paths
# the planner defaults to when it runs from the repo root in CI.
ARTIFACT_TARGETS = Path("devinfra/ci/artifact_targets.json")
SKILLS_REGISTRY = Path("skills/skills_registry.json")

BIN = "bazel-out/k8-fastbuild/bin"


def make_release(pkg: str = "pkg", outputs: str = f"bb-out/{BIN}/a.whl", bazel_flags: str = "") -> Release:
    return Release(
        pkg=pkg,
        targets="//:a",
        outputs=outputs,
        tests="",
        bazel_flags=bazel_flags,
        release_metadata="false",
        metadata_platform="",
    )


def output(path: str, digest: str) -> Output:
    return Output(
        label="//:a", path=path, uri="bytestream://h/blobs/x/1", digest=digest, size=1, output_group="default"
    )


def test_the_stream_reports_the_path_without_the_download_prefix() -> None:
    """artifact_targets.json spells outputs as `bb remote build` used to land them."""
    assert bes_path(f"bb-out/{BIN}/util/x.whl") == f"{BIN}/util/x.whl"
    assert bes_path("already/relative.whl") == "already/relative.whl"


def test_a_single_asset_release_is_identified_by_its_digest() -> None:
    """The published tag is the artifact's sha256, which BES already reports —
    so the identity costs no download at all."""
    # A real digest, as BES reports it: 64 lowercase hex characters.
    digest = "2fe232c3911f0548e3b4eb3970cfb19fc9c91928592d200f8712345678901234"
    assert content_hash(make_release(), {f"{BIN}/a.whl": digest}) == digest
    assert content_tag("skill-backtrace", digest) == "skill-backtrace-2fe232c3911f"


def test_a_multi_asset_release_composes_the_same_identity_from_digests() -> None:
    """aiquota ships a wheel and a zip; its tag is a composite, not either digest."""
    release = make_release(outputs=f"bb-out/{BIN}/a.whl bb-out/{BIN}/b.zip")
    digests = {f"{BIN}/a.whl": "aa" * 32, f"{BIN}/b.zip": "bb" * 32}
    assert content_hash(release, digests) == release_content_hash_from_digests(
        [("a.whl", "aa" * 32), ("b.zip", "bb" * 32)]
    )


def test_an_output_the_build_never_reported_has_no_identity() -> None:
    """External-repo and `manual` targets are absent from a `//...` sweep by
    design (aw-importer, gterm-theme), and must take the slow path."""
    assert content_hash(make_release(), {}) is None
    partial = {f"{BIN}/a.whl": "aa" * 32}
    assert content_hash(make_release(outputs=f"bb-out/{BIN}/a.whl bb-out/{BIN}/b.zip"), partial) is None


def test_digests_from_keys_outputs_by_full_path() -> None:
    invocation = Invocation(outputs=[output(f"{BIN}/a.whl", "aa")], test_status={})
    assert digests_from(invocation) == {f"{BIN}/a.whl": "aa"}


def test_published_release_is_dropped() -> None:
    decided = plan([make_release()], {f"{BIN}/a.whl": "aa" * 32}, lambda _: True, workers=2)
    assert matrix_include(decided) == []


def test_unpublished_release_is_kept() -> None:
    decided = plan([make_release()], {f"{BIN}/a.whl": "aa" * 32}, lambda _: False, workers=2)
    assert [row["pkg"] for row in matrix_include(decided)] == ["pkg"]


def test_the_tag_looked_up_is_the_content_tag() -> None:
    seen: list[str] = []

    def record(tag: str) -> bool:
        seen.append(tag)
        return False

    is_published(make_release(), {f"{BIN}/a.whl": "0123456789abcdef" * 4}, record)
    assert seen == ["pkg-0123456789ab"]


def test_an_unreported_output_keeps_the_release() -> None:
    """Fail open: dropping a release silently is far worse than one wasted job."""
    decided = plan([make_release()], {}, lambda _: True, workers=2)
    assert [row["pkg"] for row in matrix_include(decided)] == ["pkg"]


def test_a_failing_tag_lookup_keeps_the_release() -> None:
    def explode(_: str) -> bool:
        raise subprocess.SubprocessError("gh exploded")

    assert not is_published(make_release(), {f"{BIN}/a.whl": "aa" * 32}, explode)


def test_a_programming_error_still_crashes() -> None:
    """Fail-open covers IO and tooling faults, not bugs."""

    def bug(_: str) -> bool:
        raise TypeError("this is a bug, not a hiccup")

    with pytest.raises(TypeError):
        is_published(make_release(), {f"{BIN}/a.whl": "aa" * 32}, bug)


def test_custom_bazel_flags_never_take_the_fast_path() -> None:
    """debundle builds under -c opt, so bazel-ci's outputs are not its outputs."""
    custom = make_release(pkg="debundle", bazel_flags="-c opt")
    digests = {f"{BIN}/a.whl": "aa" * 32}
    assert not is_published(custom, digests, lambda _: True)
    assert [row["pkg"] for row in matrix_include(plan([custom], digests, lambda _: True, workers=2))] == ["debundle"]


def test_skip_ci_releases_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    main(
        [
            "--artifact-targets",
            str(ARTIFACT_TARGETS),
            "--skills-registry",
            str(SKILLS_REGISTRY),
            "--invocations",
            "",
            "--commit-subject",
            "chore: update images [skip ci]",
        ]
    )
    written = output_file.read_text()
    assert "count=0" in written
    assert json.loads(written.split("matrix=", 1)[1].splitlines()[0]) == {"include": []}


def test_no_invocation_releases_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`release` runs under always(), so a skipped or failed bazel-ci must publish
    everything rather than silently publish nothing."""
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    main(
        [
            "--artifact-targets",
            str(ARTIFACT_TARGETS),
            "--skills-registry",
            str(SKILLS_REGISTRY),
            "--invocations",
            "",
            "--commit-subject",
            "feat: something",
        ]
    )
    written = output_file.read_text()
    rows = json.loads(written.split("matrix=", 1)[1].splitlines()[0])["include"]
    assert len(rows) == len(load_releases(ARTIFACT_TARGETS, SKILLS_REGISTRY))


def test_every_pin_and_skill_reaches_exactly_one_row() -> None:
    """No release may be silently unrepresentable in the matrix."""
    doc = json.loads(ARTIFACT_TARGETS.read_text())
    skills = json.loads(SKILLS_REGISTRY.read_text())["skills"]
    rows = load_releases(ARTIFACT_TARGETS, SKILLS_REGISTRY)

    emitted_targets = [target for row in rows for target in row.targets.split()]
    assert len(emitted_targets) == len(set(emitted_targets)), "a target is claimed by two rows"
    for pin in doc["pins"].values():
        assert pin["target"] in emitted_targets
    for skill in skills:
        assert skill["target"] in emitted_targets
    assert len(rows) == len(doc["releases"]) + len(skills)


def test_each_row_pairs_one_output_with_one_target() -> None:
    for row in load_releases(ARTIFACT_TARGETS, SKILLS_REGISTRY):
        assert len(row.targets.split()) == len(row.output_paths), row.pkg
        assert row.release_metadata in {"true", "false"}, row.pkg


if __name__ == "__main__":
    pytest_bazel.main()
