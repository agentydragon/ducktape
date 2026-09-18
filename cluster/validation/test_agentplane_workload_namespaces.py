"""A bearer the central proxy admits is one the LLM ingress admits too.

A workload's token is authenticated twice on its way to a model: the proxy resolves it, decides the
request against its bindings, and substitutes it as the credential the ingress then resolves again.
Both read an allowlist from their own settings file, so a namespace in the proxy's and missing from
the ingress's is admitted for one hop and refused with 401 on the next -- a misconfiguration no
single manifest looks wrong in. These assert the relation between the two rather than either one's
roster, so adding a workload namespace to both stays silent and adding it to one fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

# Both services name their settings `Settings`, and these tests parse one of each.
from x.agentplane.egress.main import Settings as EgressSettings
from x.agentplane.llm_ingress.main import Settings as IngressSettings

# pytest_plugins loads cluster.validation.agentplane_fixtures by name; gazelle cannot see
# the dependency.
# gazelle:include_dep //cluster/validation:agentplane_fixtures
pytest_plugins = ("cluster.validation.agentplane_fixtures",)

NAMESPACES = ["agentplane-staging", "agentplane-testing"]
# The settings each Deployment supplies as an environment variable rather than a flag or the file.
DATABASE_URL = "--database-url=postgresql://validation-test/validation-test"
LITELLM_KEY = "--litellm-key=validation-test-not-a-key"

# (Deployment name, container name, config-file env var, settings ConfigMap name)
PROXY = ("agentplane-egress", "proxy", "AGENTPLANE_EGRESS_CONFIG_FILE", "agentplane-egress-settings")
INGRESS = ("agentplane-llm-ingress", "ingress", "AGENTPLANE_LLM_INGRESS_CONFIG_FILE", "agentplane-llm-ingress-settings")


def _container(documents: list[dict[str, Any]], deployment_name: str, container_name: str) -> dict[str, Any]:
    deployment = one(
        doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == deployment_name
    )
    pod: dict[str, Any] = deployment["spec"]["template"]["spec"]
    return one(candidate for candidate in pod["containers"] if candidate["name"] == container_name)


def _settings_file(tmp_path: Path, documents: list[dict[str, Any]], deployment_name: str, configmap_name: str) -> Path:
    config_map = one(
        doc for doc in documents if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == configmap_name
    )
    config_file = tmp_path / f"{deployment_name}-settings.yaml"
    config_file.write_text(config_map["data"]["settings.yaml"])
    return config_file


def _proxy(tmp_path: Path, documents: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> EgressSettings:
    deployment_name, container_name, env, cm_name = PROXY
    monkeypatch.setenv(env, str(_settings_file(tmp_path, documents, deployment_name, cm_name)))
    return EgressSettings(
        _cli_parse_args=[*_container(documents, deployment_name, container_name)["args"], DATABASE_URL]
    )


def _ingress(tmp_path: Path, documents: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> IngressSettings:
    deployment_name, container_name, env, cm_name = INGRESS
    monkeypatch.setenv(env, str(_settings_file(tmp_path, documents, deployment_name, cm_name)))
    return IngressSettings(
        _cli_parse_args=[*_container(documents, deployment_name, container_name)["args"], LITELLM_KEY]
    )


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_the_ingress_admits_every_namespace_the_proxy_does(
    namespace: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    documents = agentplane_manifests[namespace]
    proxy = _proxy(tmp_path, documents, monkeypatch)
    ingress = _ingress(tmp_path, documents, monkeypatch)

    assert proxy.allowed_service_account_namespaces <= ingress.allowed_service_account_namespaces, (
        "the proxy authenticates a workload and sends it to the ingress, which authenticates the same "
        "bearer again, so a namespace the proxy admits and the ingress does not is refused mid-hop"
    )


if __name__ == "__main__":
    pytest_bazel.main()
