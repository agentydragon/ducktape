import json
from pathlib import Path
from unittest.mock import patch

import pytest_bazel

from devinfra.ci import artifacts


def test_live_artifact_packages_are_unique() -> None:
    assert len({artifact.pkg for artifact in artifacts.ARTIFACTS}) == len(artifacts.ARTIFACTS)


def test_release_artifacts_use_the_release_tag_prefix(tmp_path: Path) -> None:
    registry = tmp_path / "targets.json"
    registry.write_text(
        json.dumps(
            {
                "pins": {
                    "standalone": {"filename": "standalone.whl", "release": "standalone"},
                    "shared": {"filename": "shared.whl", "release": "bundle"},
                }
            }
        )
    )
    with patch.object(artifacts, "artifact_targets_path", return_value=registry):
        projected = artifacts._release_artifacts()

    assert [(item.pkg, item.filename, item.release_tag_prefix) for item in projected] == [
        ("standalone", "standalone.whl", "standalone"),
        ("shared", "shared.whl", "bundle"),
    ]


def test_skill_artifacts_include_only_nix_enabled_skills(tmp_path: Path) -> None:
    registry = tmp_path / "skills.json"
    registry.write_text(
        json.dumps(
            {
                "skills": [
                    {"pkg": "default", "filename": "default.skill"},
                    {"pkg": "enabled", "filename": "enabled.skill", "nix": True},
                    {"pkg": "disabled", "filename": "disabled.skill", "nix": False},
                ]
            }
        )
    )
    with patch.object(artifacts, "skills_registry_path", return_value=registry):
        projected = artifacts._skill_artifacts()

    assert [(item.pkg, item.filename, item.release_tag_prefix) for item in projected] == [
        ("default", "default.skill", "default"),
        ("enabled", "enabled.skill", "enabled"),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
