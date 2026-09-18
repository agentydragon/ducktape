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
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path

# Both services name their settings `Settings`, and these tests parse one of each.
from x.agentplane.egress.main import Settings as EgressSettings
from x.agentplane.llm_ingress.main import Settings as IngressSettings

NAMESPACES = ["agentplane-staging", "agentplane-testing"]
# The settings each Deployment supplies as an environment variable rather than a flag or the file.
DATABASE_URL = "--database-url=postgresql://validation-test/validation-test"
LITELLM_KEY = "--litellm-key=validation-test-not-a-key"

PROXY = ("egress", "agentplane-egress.k8s.yaml", "proxy", "AGENTPLANE_EGRESS_CONFIG_FILE", "agentplane-egress-settings")
INGRESS = (
    "llm-ingress",
    "agentplane-llm-ingress.k8s.yaml",
    "ingress",
    "AGENTPLANE_LLM_INGRESS_CONFIG_FILE",
    "agentplane-llm-ingress-settings",
)


def _documents(namespace: str, directory: str, manifest: str) -> list[dict[str, Any]]:
    path = get_required_path(f"_main/cluster/k8s/{namespace}/{directory}/{manifest}")
    return list(yaml.safe_load_all(Path(path).read_text()))


def _container(namespace: str, directory: str, manifest: str, name: str) -> dict[str, Any]:
    deployment = one(doc for doc in _documents(namespace, directory, manifest) if doc["kind"] == "Deployment")
    pod: dict[str, Any] = deployment["spec"]["template"]["spec"]
    return one(candidate for candidate in pod["containers"] if candidate["name"] == name)


def _settings_file(tmp_path: Path, namespace: str, directory: str, manifest: str, configmap_name: str) -> Path:
    config_map = one(
        doc
        for doc in _documents(namespace, directory, manifest)
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == configmap_name
    )
    config_file = tmp_path / f"{directory}-settings.yaml"
    config_file.write_text(config_map["data"]["settings.yaml"])
    return config_file


def _proxy(tmp_path: Path, namespace: str, monkeypatch: pytest.MonkeyPatch) -> EgressSettings:
    directory, manifest, name, env, cm_name = PROXY
    monkeypatch.setenv(env, str(_settings_file(tmp_path, namespace, directory, manifest, cm_name)))
    return EgressSettings(_cli_parse_args=[*_container(namespace, directory, manifest, name)["args"], DATABASE_URL])


def _ingress(tmp_path: Path, namespace: str, monkeypatch: pytest.MonkeyPatch) -> IngressSettings:
    directory, manifest, name, env, cm_name = INGRESS
    monkeypatch.setenv(env, str(_settings_file(tmp_path, namespace, directory, manifest, cm_name)))
    return IngressSettings(_cli_parse_args=[*_container(namespace, directory, manifest, name)["args"], LITELLM_KEY])


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_the_ingress_admits_every_namespace_the_proxy_does(
    namespace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = _proxy(tmp_path, namespace, monkeypatch)
    ingress = _ingress(tmp_path, namespace, monkeypatch)

    assert proxy.allowed_service_account_namespaces <= ingress.allowed_service_account_namespaces, (
        "the proxy authenticates a workload and sends it to the ingress, which authenticates the same "
        "bearer again, so a namespace the proxy admits and the ingress does not is refused mid-hop"
    )


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_both_read_the_same_projected_token_audience(
    namespace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substituted credential is the workload's own token, so one audience has to satisfy both."""
    assert (
        _proxy(tmp_path, namespace, monkeypatch).token_audience
        == _ingress(tmp_path, namespace, monkeypatch).token_audience
    )


@pytest.mark.parametrize("namespace", NAMESPACES)
@pytest.mark.parametrize(("directory", "manifest", "name", "env", "cm_name"), [PROXY, INGRESS])
def test_each_deployment_mounts_the_settings_file_it_names(
    namespace: str, directory: str, manifest: str, name: str, env: str, cm_name: str
) -> None:
    """The env var names a path inside the container; the mount is what puts a file there. They are
    written in different blocks of the same manifest, and the service refuses to start on a path
    that is not a regular file -- so disagreeing spellings are a CrashLoopBackOff, not a default."""
    del cm_name  # only needed by _settings_file, not this manifest-shape assertion
    container = _container(namespace, directory, manifest, name)
    configured = one(entry for entry in container["env"] if entry["name"] == env)["value"]
    mount = one(entry for entry in container["volumeMounts"] if entry["mountPath"] == configured)

    assert mount["subPath"] == "settings.yaml", "a whole-directory mount would hide the key's file"
    assert mount["readOnly"] is True


if __name__ == "__main__":
    pytest_bazel.main()
