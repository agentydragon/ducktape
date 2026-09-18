"""`Settings` edges around the mounted settings file."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from x.agentplane.egress.main import Settings

CONFIG_FILE_ENV = "AGENTPLANE_EGRESS_CONFIG_FILE"


def _args(tmp_path: Path) -> list[str]:
    return [
        "--rules-namespace=egress-test",
        f"--ca-cert={tmp_path / 'ca.crt'}",
        f"--ca-key={tmp_path / 'ca.key'}",
        f"--confdir={tmp_path}",
        "--database-url=postgresql://egress-test.invalid/egress-test",
    ]


def test_a_second_workload_namespace_is_another_list_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What hosting an agent elsewhere costs: one more entry in the settings file."""
    config = tmp_path / "settings.yaml"
    config.write_text("allowed_service_account_namespaces:\n  - egress-test\n  - other-workloads\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))

    settings = Settings(_cli_parse_args=_args(tmp_path))

    assert settings.allowed_service_account_namespaces == frozenset({"egress-test", "other-workloads"})


def test_a_deployment_naming_no_workload_namespace_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist is what lets a bearer be presented at all, so an empty one accepts nothing and
    is a misconfiguration to fail on at startup rather than serve."""
    config = tmp_path / "settings.yaml"
    config.write_text("{}\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))

    with pytest.raises(ValidationError, match="allowed_service_account_namespaces"):
        Settings(_cli_parse_args=_args(tmp_path))


def test_a_settings_file_that_is_not_there_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-settings ignores an absent YAML file, which would leave a bound deployment running on
    defaults. A path the deployment names and the cluster does not mount has to be fatal instead."""
    monkeypatch.setenv(CONFIG_FILE_ENV, str(tmp_path / "never-written.yaml"))

    with pytest.raises(ValueError, match="not a regular file"):
        Settings(_cli_parse_args=_args(tmp_path))


if __name__ == "__main__":
    pytest_bazel.main()
