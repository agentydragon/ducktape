import json

import pytest_bazel

from devinfra.ci.artifacts import ARTIFACTS, ArtifactTargets, artifact_targets_path, skills_registry_path


def test_artifacts_are_derived_from_release_metadata() -> None:
    artifacts = {artifact.pkg: artifact for artifact in ARTIFACTS}
    targets = ArtifactTargets.model_validate_json(artifact_targets_path().read_text()).pins
    skills = json.loads(skills_registry_path().read_text())["skills"]

    assert len(artifacts) == len(ARTIFACTS)
    assert set(artifacts) == set(targets) | {skill["pkg"] for skill in skills}
    for pkg, target in targets.items():
        assert artifacts[pkg].filename == target.filename
        assert artifacts[pkg].release_tag_prefix == target.release
    for skill in skills:
        assert artifacts[skill["pkg"]].filename == skill["filename"]


if __name__ == "__main__":
    pytest_bazel.main()
