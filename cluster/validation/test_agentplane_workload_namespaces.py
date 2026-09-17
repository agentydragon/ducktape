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

PROXY = ("egress", "deployment-agentplane-egress.yaml", "proxy", "AGENTPLANE_EGRESS_CONFIG_FILE")
INGRESS = ("llm-ingress", "deployment.yaml", "ingress", "AGENTPLANE_LLM_INGRESS_CONFIG_FILE")


def _container(namespace: str, directory: str, manifest: str, name: str) -> dict[str, Any]:
    path = get_required_path(f"_main/cluster/k8s/{namespace}/{directory}/{manifest}")
    pod: dict[str, Any] = yaml.safe_load(Path(path).read_text())["spec"]["template"]["spec"]
    return one(candidate for candidate in pod["containers"] if candidate["name"] == name)


def _settings_file(namespace: str, directory: str) -> Path:
    return Path(get_required_path(f"_main/cluster/k8s/{namespace}/{directory}/settings.yaml"))


def _proxy(namespace: str, monkeypatch: pytest.MonkeyPatch) -> EgressSettings:
    directory, manifest, name, env = PROXY
    monkeypatch.setenv(env, str(_settings_file(namespace, directory)))
    return EgressSettings(_cli_parse_args=[*_container(namespace, directory, manifest, name)["args"], DATABASE_URL])


def _ingress(namespace: str, monkeypatch: pytest.MonkeyPatch) -> IngressSettings:
    directory, manifest, name, env = INGRESS
    monkeypatch.setenv(env, str(_settings_file(namespace, directory)))
    return IngressSettings(_cli_parse_args=[*_container(namespace, directory, manifest, name)["args"], LITELLM_KEY])


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_the_ingress_admits_every_namespace_the_proxy_does(namespace: str, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = _proxy(namespace, monkeypatch)
    ingress = _ingress(namespace, monkeypatch)

    assert proxy.allowed_service_account_namespaces <= ingress.allowed_service_account_namespaces, (
        "the proxy authenticates a workload and sends it to the ingress, which authenticates the same "
        "bearer again, so a namespace the proxy admits and the ingress does not is refused mid-hop"
    )


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_both_read_the_same_projected_token_audience(namespace: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The substituted credential is the workload's own token, so one audience has to satisfy both."""
    assert _proxy(namespace, monkeypatch).token_audience == _ingress(namespace, monkeypatch).token_audience


@pytest.mark.parametrize("namespace", NAMESPACES)
@pytest.mark.parametrize(("directory", "manifest", "name", "env"), [PROXY, INGRESS])
def test_each_deployment_mounts_the_settings_file_it_names(
    namespace: str, directory: str, manifest: str, name: str, env: str
) -> None:
    """The env var names a path inside the container; the mount is what puts a file there. They are
    written in different blocks of the same manifest, and the service refuses to start on a path
    that is not a regular file -- so disagreeing spellings are a CrashLoopBackOff, not a default."""
    container = _container(namespace, directory, manifest, name)
    configured = one(entry for entry in container["env"] if entry["name"] == env)["value"]
    mount = one(entry for entry in container["volumeMounts"] if entry["mountPath"] == configured)

    assert mount["subPath"] == "settings.yaml", "a whole-directory mount would hide the key's file"
    assert mount["readOnly"] is True


if __name__ == "__main__":
    pytest_bazel.main()
