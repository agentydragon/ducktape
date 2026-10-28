"""Tests for reading the mounted shared Restic credential source."""

from pathlib import Path

import pytest
import pytest_bazel
import yaml

from x.ai_session_archive.cli import _restic_environment


def test_restic_environment_reads_only_the_requested_machine_credentials(tmp_path: Path) -> None:
    credentials_file = tmp_path / "session-backup.yaml"
    credentials_file.write_text(
        yaml.safe_dump(
            {
                "restic_rest_username_rugged": "test-rugged-user",
                "restic_rest_password_rugged": "test-rugged-password",
                "restic_rest_username_wyrm2": "test-wyrm2-user",
                "restic_rest_password_wyrm2": "test-wyrm2-password",
                "restic_password": "test-repository-password",
            }
        )
    )
    environment = {"RESTIC_REPOSITORY": "rest:https://backup.test/rugged", "RESTIC_CACHE_DIR": "/tmp/cache"}

    result = _restic_environment(machine_id="rugged", credentials_file=credentials_file, environment=environment)

    assert result == {
        **environment,
        "RESTIC_REST_USERNAME": "test-rugged-user",
        "RESTIC_REST_PASSWORD": "test-rugged-password",
        "RESTIC_PASSWORD": "test-repository-password",
    }
    assert environment.keys() == {"RESTIC_REPOSITORY", "RESTIC_CACHE_DIR"}


def test_restic_environment_rejects_incomplete_machine_credentials(tmp_path: Path) -> None:
    credentials_file = tmp_path / "session-backup.yaml"
    credentials_file.write_text("restic_password: test-repository-password\n")

    with pytest.raises(ValueError, match="missing a non-empty Restic credential"):
        _restic_environment(machine_id="rugged", credentials_file=credentials_file, environment={})


def test_restic_environment_rejects_mixed_file_and_environment_credentials(tmp_path: Path) -> None:
    credentials_file = tmp_path / "session-backup.yaml"
    credentials_file.write_text("restic_password: test-repository-password\n")

    with pytest.raises(ValueError, match="through --credentials-file or RESTIC_\* environment variables"):
        _restic_environment(
            machine_id="rugged",
            credentials_file=credentials_file,
            environment={"RESTIC_PASSWORD_FILE": "/tmp/password"},
        )


if __name__ == "__main__":
    pytest_bazel.main()
