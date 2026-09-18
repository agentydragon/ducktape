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
from more_itertools import one

from x.agentplane.egress.main import Settings

# pytest_plugins loads cluster.validation.agentplane_fixtures by name; gazelle cannot see
# the dependency.
# gazelle:include_dep //cluster/validation:agentplane_fixtures
pytest_plugins = ("cluster.validation.agentplane_fixtures",)

NAMESPACES = ["agentplane-staging", "agentplane-testing"]
CONFIG_FILE_ENV = "AGENTPLANE_EGRESS_CONFIG_FILE"
# The one setting the manifests supply as an environment variable rather than a flag or the file.
DATABASE_URL = "--database-url=postgresql://validation-test/validation-test"


def _proxy_args(documents: list[dict[str, Any]]) -> list[str]:
    deployment = one(
        doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "agentplane-egress"
    )
    pod: dict[str, Any] = deployment["spec"]["template"]["spec"]
    return list(one(container for container in pod["containers"] if container["name"] == "proxy")["args"])


def _settings_file(tmp_path: Path, documents: list[dict[str, Any]]) -> Path:
    config_map = one(
        doc
        for doc in documents
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-egress-settings"
    )
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(config_map["data"]["settings.yaml"])
    return config_file


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_the_deployed_configuration_parses_into_settings(
    namespace: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    documents = agentplane_manifests[namespace]
    monkeypatch.setenv(CONFIG_FILE_ENV, str(_settings_file(tmp_path, documents)))

    settings = Settings(_cli_parse_args=[*_proxy_args(documents), DATABASE_URL])

    assert settings.rules_namespace == namespace
    assert namespace in settings.allowed_service_account_namespaces, (
        "the sandboxes run beside the rules, so their namespace has to be one a bearer may come from"
    )


if __name__ == "__main__":
    pytest_bazel.main()
