"""Contracts between public-coder's constructs and its hand-written iron config. The
constructs' own relations are `//cluster/cdk8s:test_public_coder_agent_config`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import pytest
import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s import public_coder_agent_config


def _one(objects: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return one(obj for obj in objects if obj["kind"] == kind)


@pytest.fixture(scope="module")
def app_objects() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(public_coder_agent_config.app_chart(Cdk8sTesting.app())))


@pytest.fixture(scope="module")
def kubeconfig() -> dict[str, Any]:
    objects = Cdk8sTesting.synth(public_coder_agent_config.kubeconfig_chart(Cdk8sTesting.app()))
    return cast(dict[str, Any], yaml.safe_load(_one(objects, "ConfigMap")["data"]["config"]))


def test_public_coder_kubernetes_proxy_contract(
    k8s_dir: Path, app_objects: list[dict[str, Any]], kubeconfig: dict[str, Any]
) -> None:
    """The kubeconfig and the app's placeholders agree with what iron substitutes."""
    iron = yaml.safe_load((k8s_dir / "agents" / "public-coder-agent" / "proxy" / "iron.yaml").read_text())
    secrets_transform = one(transform for transform in iron["transforms"] if transform["name"] == "secrets")
    secrets_by_env = {entry["source"]["var"]: entry for entry in secrets_transform["config"]["secrets"]}

    # The kubeconfig carries the placeholder iron swaps for the Console bearer, on the host it
    # targets.
    haku_secret = secrets_by_env["HAKU_CONSOLE_TOKEN"]
    assert one(kubeconfig["users"])["user"]["token"] == haku_secret["replace"]["proxy_value"]
    server_host = urlparse(one(kubeconfig["clusters"])["cluster"]["server"]).hostname
    assert server_host in {rule["host"] for rule in haku_secret["rules"]}

    # The app holds only the placeholders iron replaces; the proxy holds the credentials.
    app_container = one(_one(app_objects, "Deployment")["spec"]["template"]["spec"]["containers"])
    app_env = {entry["name"]: entry for entry in app_container["env"]}
    github_placeholder = secrets_by_env["GITHUB_TOKEN"]["replace"]["proxy_value"]
    assert app_env["GITHUB_TOKEN"]["value"] == github_placeholder
    assert app_env["GH_PAT"]["value"] == github_placeholder
    assert (
        app_env["AIQUOTA_API_BEARER_TOKEN"]["value"]
        == secrets_by_env["AIQUOTA_API_BEARER_TOKEN"]["replace"]["proxy_value"]
    )


if __name__ == "__main__":
    pytest_bazel.main()
