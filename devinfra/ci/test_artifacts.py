import json
from pathlib import Path

import pytest_bazel

from devinfra.ci.artifacts import ARTIFACTS, ArtifactTargets, artifact_targets_path, skills_registry_path


def test_artifacts_are_derived_from_release_metadata() -> None:
    artifacts = {artifact.pkg: artifact for artifact in ARTIFACTS}
    targets = ArtifactTargets.model_validate_json(artifact_targets_path().read_text()).pins
    skills = json.loads(skills_registry_path().read_text())["skills"]

    assert len(artifacts) == len(ARTIFACTS)
    assert set(artifacts) == set(targets) | {skill["pkg"] for skill in skills}
    for pkg, target in targets.items():
        assert artifacts[pkg].filename == Path(target.output).name
        assert artifacts[pkg].release_tag_prefix == target.release
    for skill in skills:
        assert artifacts[skill["pkg"]].filename == skill["filename"]


def test_debundle_release_ships_the_sidecar_the_binary_looks_for() -> None:
    """A downloaded `debundle` finds its CP-SAT sidecar by file name beside itself
    (devinfra/js/debundle/docs/cli.md), so the release must carry it under that name."""
    assets = {artifact.filename for artifact in ARTIFACTS if artifact.release_tag_prefix == "debundle"}
    assert {"debundle", "selector_cpsat_solver"} <= assets


if __name__ == "__main__":
    pytest_bazel.main()
