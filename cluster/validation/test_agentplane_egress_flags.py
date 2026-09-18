"""The configuration the egress Deployment supplies is configuration the egress binary accepts.

A renamed or dropped setting is otherwise found by a CrashLoopBackOff. The flags and the mounted
settings file are checked against the parser together, because neither half is the whole
configuration: scalars arrive as `args`, and `allowed_service_account_namespaces` arrives as a YAML
list from the ConfigMap, where a list needs no encoding to survive a Deployment's `args`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one
from pydantic import ValidationError

from util.bazel.runfiles import get_required_path
from x.agentplane.egress.main import Settings

NAMESPACES = ["agentplane-staging", "agentplane-testing"]
CONFIG_FILE_ENV = "AGENTPLANE_EGRESS_CONFIG_FILE"
# The one setting the manifests supply as an environment variable rather than a flag or the file.
DATABASE_URL = "--database-url=postgresql://validation-test/validation-test"


def _egress_documents(namespace: str) -> list[dict[str, Any]]:
    manifest = get_required_path(f"_main/cluster/k8s/{namespace}/egress/agentplane-egress.k8s.yaml")
    return list(yaml.safe_load_all(Path(manifest).read_text()))


def _proxy_args(namespace: str) -> list[str]:
    deployment = one(doc for doc in _egress_documents(namespace) if doc["kind"] == "Deployment")
    pod: dict[str, Any] = deployment["spec"]["template"]["spec"]
    return list(one(container for container in pod["containers"] if container["name"] == "proxy")["args"])


def _settings_file(tmp_path: Path, namespace: str) -> Path:
    config_map = one(
        doc
        for doc in _egress_documents(namespace)
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-egress-settings"
    )
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(config_map["data"]["settings.yaml"])
    return config_file


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_the_deployed_configuration_parses_into_settings(
    namespace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_FILE_ENV, str(_settings_file(tmp_path, namespace)))

    settings = Settings(_cli_parse_args=[*_proxy_args(namespace), DATABASE_URL])

    assert settings.rules_namespace == namespace
    assert namespace in settings.allowed_service_account_namespaces, (
        "the sandboxes run beside the rules, so their namespace has to be one a bearer may come from"
    )


def test_a_second_workload_namespace_is_another_list_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What hosting an agent elsewhere costs: one more entry in the settings file."""
    config = tmp_path / "settings.yaml"
    config.write_text("allowed_service_account_namespaces:\n  - agentplane-staging\n  - public-coder\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))

    settings = Settings(_cli_parse_args=[*_proxy_args("agentplane-staging"), DATABASE_URL])

    assert settings.allowed_service_account_namespaces == frozenset({"agentplane-staging", "public-coder"})


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_a_deployment_naming_no_workload_namespace_is_refused(
    namespace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist is what lets a bearer be presented at all, so an empty one accepts nothing and
    is a misconfiguration to fail on at startup rather than serve."""
    config = tmp_path / "settings.yaml"
    config.write_text("{}\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))

    with pytest.raises(ValidationError, match="allowed_service_account_namespaces"):
        Settings(_cli_parse_args=[*_proxy_args(namespace), DATABASE_URL])


def test_a_settings_file_that_is_not_there_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-settings ignores an absent YAML file, which would leave a bound deployment running on
    defaults. A path the deployment names and the cluster does not mount has to be fatal instead."""
    monkeypatch.setenv(CONFIG_FILE_ENV, str(tmp_path / "never-written.yaml"))

    with pytest.raises(ValueError, match="not a regular file"):
        Settings(_cli_parse_args=[*_proxy_args("agentplane-staging"), DATABASE_URL])


if __name__ == "__main__":
    pytest_bazel.main()
