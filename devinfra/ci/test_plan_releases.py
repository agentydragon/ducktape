import json
import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.plan_releases import Release, content_tag, is_published, load_releases, main, matrix_include, plan
from devinfra.ci.release_content_hash import release_content_hash

# Relative to the runfiles root, which is cwd under `bazel test` — the same paths
# the planner defaults to when it runs from the repo root in CI.
ARTIFACT_TARGETS = Path("devinfra/ci/artifact_targets.json")
SKILLS_REGISTRY = Path("skills/skills_registry.json")


def make_release(pkg: str = "pkg", outputs: str = "bb-out/a.whl", bazel_flags: str = "") -> Release:
    return Release(
        pkg=pkg,
        targets="//:a",
        outputs=outputs,
        tests="",
        bazel_flags=bazel_flags,
        release_metadata="false",
        metadata_platform="",
    )


def write(root: Path, relative: str, content: bytes = b"payload") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_tag_is_the_hash_release_artifact_already_computes() -> None:
    """The identity must stay byte-identical or every package republishes once."""
    artifacts = [Path(__file__)]
    assert content_tag("bbapi", artifacts) == f"bbapi-{release_content_hash(artifacts)[:12]}"


def test_published_release_is_dropped(tmp_path: Path) -> None:
    write(tmp_path, "bb-out/a.whl")
    decided = plan([make_release()], tmp_path, lambda _: True, workers=2)
    assert matrix_include(decided) == []


def test_unpublished_release_is_kept(tmp_path: Path) -> None:
    write(tmp_path, "bb-out/a.whl")
    decided = plan([make_release()], tmp_path, lambda _: False, workers=2)
    assert [row["pkg"] for row in matrix_include(decided)] == ["pkg"]


def test_the_tag_looked_up_is_the_content_tag(tmp_path: Path) -> None:
    artifact = write(tmp_path, "bb-out/a.whl", b"exact bytes")
    seen: list[str] = []

    def record(tag: str) -> bool:
        seen.append(tag)
        return False

    is_published(make_release(), tmp_path, record)
    assert seen == [f"pkg-{release_content_hash([artifact])[:12]}"]


def test_missing_artifact_keeps_the_release(tmp_path: Path) -> None:
    """Fail open: dropping a release silently is far worse than one wasted job."""
    decided = plan([make_release()], tmp_path, lambda _: True, workers=2)
    assert [row["pkg"] for row in matrix_include(decided)] == ["pkg"]


def test_a_failing_tag_lookup_keeps_the_release(tmp_path: Path) -> None:
    write(tmp_path, "bb-out/a.whl")

    def explode(_: str) -> bool:
        raise subprocess.SubprocessError("gh exploded")

    assert not is_published(make_release(), tmp_path, explode)


def test_a_programming_error_still_crashes(tmp_path: Path) -> None:
    """Fail-open covers IO and tooling faults, not bugs."""
    write(tmp_path, "bb-out/a.whl")

    def bug(_: str) -> bool:
        raise TypeError("this is a bug, not a hiccup")

    with pytest.raises(TypeError):
        is_published(make_release(), tmp_path, bug)


def test_custom_bazel_flags_never_take_the_fast_path(tmp_path: Path) -> None:
    """A row built under another config wasn't in the plan build, so it can't be judged."""
    write(tmp_path, "bb-out/a.whl")
    custom = make_release(pkg="debundle", bazel_flags="-c opt")
    assert not is_published(custom, tmp_path, lambda _: True)
    assert [row["pkg"] for row in matrix_include(plan([custom], tmp_path, lambda _: True, workers=2))] == ["debundle"]


def test_targets_subcommand_omits_custom_config_rows(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--artifact-targets", str(ARTIFACT_TARGETS), "--skills-registry", str(SKILLS_REGISTRY), "targets"])
    printed = capsys.readouterr().out.split()
    releases = load_releases(ARTIFACT_TARGETS, SKILLS_REGISTRY)
    custom = [r for r in releases if not r.uses_default_config]
    for release in custom:
        for target in release.targets.split():
            assert target not in printed
    for release in releases:
        if release.uses_default_config:
            assert all(target in printed for target in release.targets.split())


def test_skip_ci_releases_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    main(
        [
            "--artifact-targets",
            str(ARTIFACT_TARGETS),
            "--skills-registry",
            str(SKILLS_REGISTRY),
            "plan",
            "--root",
            str(tmp_path),
            "--commit-subject",
            "chore: update images [skip ci]",
        ]
    )
    written = output.read_text()
    assert "count=0" in written
    assert json.loads(written.split("matrix=", 1)[1].splitlines()[0]) == {"include": []}


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
        assert len(row.targets.split()) == len(row.outputs.split()), row.pkg
        assert row.release_metadata in {"true", "false"}, row.pkg


if __name__ == "__main__":
    pytest_bazel.main()
