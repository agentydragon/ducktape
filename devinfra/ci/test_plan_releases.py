import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci.bes import Output
from devinfra.ci.plan_releases import content_hash, is_published, main, matrix_include, plan
from devinfra.ci.release_assets import Release, load_releases
from devinfra.ci.release_content_hash import release_content_hash_from_digests

# Relative to the runfiles root, which is cwd under `bazel test` — the same paths
# the planner defaults to when it runs from the repo root in CI.
ARTIFACT_TARGETS = Path("devinfra/ci/artifact_targets.json")
SKILLS_REGISTRY = Path("skills/skills_registry.json")

BIN = "bazel-out/k8-fastbuild/bin"


SINGLE_ASSET = Release(pkg="pkg", assets=(("//:a", "a.whl"),))
TWO_ASSETS = Release(pkg="bundle", assets=(("//:a", "a.whl"), ("//:b", "b.zip")))


def output(path: str, digest: str, label: str = "//:a", output_group: str = "default", aspect: str = "") -> Output:
    return Output(
        label=label, path=path, uri="bytestream://h/blobs/x/1", digest=digest, output_group=output_group, aspect=aspect
    )


def test_a_single_asset_release_is_identified_by_its_digest() -> None:
    """The published tag is the artifact's sha256, which BES already reports —
    so the identity costs no download at all."""
    # A real digest, as BES reports it: 64 lowercase hex characters.
    digest = "2fe232c3911f0548e3b4eb3970cfb19fc9c91928592d200f8712345678901234"
    assert content_hash(SINGLE_ASSET, {"//:a": [output(f"{BIN}/a.whl", digest)]}) == digest


def test_a_multi_asset_release_composes_the_same_identity_from_digests() -> None:
    """aiquota ships a wheel and a zip; its tag is a composite, not either digest."""
    by_label = {"//:a": [output(f"{BIN}/a.whl", "aa" * 32)], "//:b": [output(f"{BIN}/b.zip", "bb" * 32, label="//:b")]}
    assert content_hash(TWO_ASSETS, by_label) == release_content_hash_from_digests(
        [("a.whl", "aa" * 32), ("b.zip", "bb" * 32)]
    )


def test_an_output_the_build_never_reported_has_no_identity() -> None:
    """External-repo and `manual` targets are absent from a `//...` sweep by
    design (aw-importer, gterm-theme), and must take the slow path."""
    assert content_hash(SINGLE_ASSET, {}) is None
    partial = {"//:a": [output(f"{BIN}/a.whl", "aa" * 32)]}
    assert content_hash(TWO_ASSETS, partial) is None


def test_lint_aspect_reports_do_not_hide_the_artifact() -> None:
    """bazel-ci runs the mypy/ruff aspects, whose report files complete under the
    same label; the identity must come from the target's own artifact."""
    by_label = {
        "//:a": [
            output(f"{BIN}/a.mypy_stdout", "ee" * 32, output_group="mypy", aspect="//devinfra/lint:mypy_aspect"),
            output(f"{BIN}/a.whl", "aa" * 32),
        ]
    }
    assert content_hash(SINGLE_ASSET, by_label) == "aa" * 32


def test_a_label_with_no_single_artifact_raises_rather_than_guessing() -> None:
    """Two default outputs, or only aspect leftovers, is SSOT drift: the planner
    warns and keeps the row, and the row's own job fails loudly on the same check."""
    ambiguous = {"//:a": [output(f"{BIN}/a.whl", "aa" * 32), output(f"{BIN}/extra.txt", "cc" * 32)]}
    with pytest.raises(ValueError, match="default outputs"):
        content_hash(SINGLE_ASSET, ambiguous)
    aspect_only = {"//:a": [output(f"{BIN}/a.mypy_stdout", "ee" * 32, output_group="mypy", aspect="mypy")]}
    with pytest.raises(ValueError, match="not reported"):
        content_hash(SINGLE_ASSET, aspect_only)
    assert not is_published(SINGLE_ASSET, ambiguous, lambda _: True)


def test_a_built_basename_contradicting_the_pin_filename_raises() -> None:
    """`filename` is the release-download URL component sync-pins builds, so a
    build producing a differently-named file must not be silently skipped."""
    by_label = {"//:a": [output(f"{BIN}/renamed.whl", "aa" * 32)]}
    with pytest.raises(ValueError, match="filename"):
        content_hash(SINGLE_ASSET, by_label)
    assert not is_published(SINGLE_ASSET, by_label, lambda _: True)


def test_matrix_row_encodes_targets_and_workflow_inputs() -> None:
    """release.yml reads the row's keys; filenames is the planner's own affair."""
    row = TWO_ASSETS.as_matrix_row()
    assert row == {
        "pkg": "bundle",
        "targets": "//:a //:b",
        "tests": "",
        "bazel_flags": "",
        "release_metadata": "false",
        "metadata_platform": "",
    }


def by_label_a(digest: str = "aa" * 32) -> dict[str, list[Output]]:
    return {"//:a": [output(f"{BIN}/a.whl", digest)]}


def test_published_release_is_dropped() -> None:
    decided = plan([SINGLE_ASSET], by_label_a(), lambda _: True, workers=2)
    assert matrix_include(decided) == []


def test_unpublished_release_is_kept() -> None:
    decided = plan([SINGLE_ASSET], by_label_a(), lambda _: False, workers=2)
    assert [row["pkg"] for row in matrix_include(decided)] == ["pkg"]


def test_a_release_whose_tests_push_ci_already_failed_gets_no_job() -> None:
    release = dataclasses.replace(SINGLE_ASSET, tests="//:a_test")
    decided = plan([release], by_label_a(), lambda _: False, workers=2)
    assert matrix_include(decided, blocked_tests={"//:a_test"}) == []


def test_the_tag_looked_up_is_the_content_tag() -> None:
    seen: list[str] = []

    def record(tag: str) -> bool:
        seen.append(tag)
        return False

    is_published(SINGLE_ASSET, by_label_a("0123456789abcdef" * 4), record)
    assert seen == ["pkg-0123456789ab"]


def test_an_unreported_output_keeps_the_release() -> None:
    """Fail open: dropping a release silently is far worse than one wasted job."""
    decided = plan([SINGLE_ASSET], {}, lambda _: True, workers=2)
    assert [row["pkg"] for row in matrix_include(decided)] == ["pkg"]


def test_a_failing_tag_lookup_keeps_the_release() -> None:
    def explode(_: str) -> bool:
        raise subprocess.SubprocessError("gh exploded")

    assert not is_published(SINGLE_ASSET, by_label_a(), explode)


def test_a_programming_error_still_crashes() -> None:
    """Fail-open covers IO and tooling faults, not bugs."""

    def bug(_: str) -> bool:
        raise TypeError("this is a bug, not a hiccup")

    with pytest.raises(TypeError):
        is_published(SINGLE_ASSET, by_label_a(), bug)


def test_custom_bazel_flags_never_take_the_fast_path() -> None:
    """debundle builds under -c opt, so bazel-ci's outputs are not its outputs."""
    custom = dataclasses.replace(SINGLE_ASSET, pkg="debundle", bazel_flags="-c opt")
    assert not is_published(custom, by_label_a(), lambda _: True)
    assert [row["pkg"] for row in matrix_include(plan([custom], by_label_a(), lambda _: True, workers=2))] == [
        "debundle"
    ]


def test_skip_ci_releases_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.chdir(tmp_path)  # A skipped commit needs neither release registry.
    main(["--commit-subject", "chore: update images [skip ci]"])
    written = output_file.read_text()
    assert "count=0" in written
    assert json.loads(written.split("matrix=", 1)[1].splitlines()[0]) == {"include": []}


def test_no_invocation_releases_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`release` runs under always(), so a skipped or failed bazel-ci must publish
    everything rather than silently publish nothing."""
    artifact_targets = json.loads(ARTIFACT_TARGETS.read_text())
    skills = json.loads(SKILLS_REGISTRY.read_text())["skills"]
    targets_by_release: dict[str, list[str]] = {}
    for pin in artifact_targets["pins"].values():
        targets_by_release.setdefault(pin["release"], []).append(pin["target"])
    expected_targets_by_pkg = {
        pkg: " ".join(targets_by_release[pkg]) for pkg in artifact_targets["releases"] if pkg in targets_by_release
    }
    expected_targets_by_pkg.update({skill["pkg"]: skill["target"] for skill in skills})

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
    actual_targets_by_pkg = {row["pkg"]: row["targets"] for row in rows}
    assert len(actual_targets_by_pkg) == len(rows), "a release package appears more than once"
    assert actual_targets_by_pkg == expected_targets_by_pkg


def test_every_pin_and_skill_reaches_exactly_one_row() -> None:
    """No release may be silently unrepresentable in the matrix."""
    doc = json.loads(ARTIFACT_TARGETS.read_text())
    skills = json.loads(SKILLS_REGISTRY.read_text())["skills"]
    rows = load_releases(ARTIFACT_TARGETS, SKILLS_REGISTRY)

    emitted_targets = [target for row in rows for target, _ in row.assets]
    assert len(emitted_targets) == len(set(emitted_targets)), "a target is claimed by two rows"
    for pin in doc["pins"].values():
        assert pin["target"] in emitted_targets
    for skill in skills:
        assert skill["target"] in emitted_targets
    assert len(rows) == len(doc["releases"]) + len(skills)


def test_registered_releases_have_valid_filenames_and_metadata_flags() -> None:
    for row in load_releases(ARTIFACT_TARGETS, SKILLS_REGISTRY):
        assert row.as_matrix_row()["release_metadata"] in {"true", "false"}, row.pkg
        for _, filename in row.assets:
            assert "/" not in filename, f"{row.pkg}: filename must be a bare asset name, not a path"


if __name__ == "__main__":
    pytest_bazel.main()
