"""Parity tests for the cdk8s LiteLLM manifest generator."""

from pathlib import Path

import pytest_bazel
import yaml

from cluster.generate_manifests import _config_maps, generate_manifests
from cluster.litellm_config import main_proxy_config
from util.bazel.runfiles import get_required_path


def _committed_config(path: str) -> dict:
    config = yaml.safe_load(get_required_path(path).read_text())
    assert isinstance(config, dict)
    return config


def test_main_proxy_config_is_generated_from_the_roster() -> None:
    assert main_proxy_config() == _committed_config("ducktape/cluster/k8s/litellm/app/proxy-config.yaml")


def test_config_map_payloads_contain_the_existing_proxy_configs() -> None:
    generated = {chart_name: (namespace, data) for chart_name, _, namespace, data in _config_maps()}
    generated_namespace, data = generated["litellm"]
    assert generated_namespace == "litellm"
    assert data["config.yaml"] == _committed_config("ducktape/cluster/k8s/litellm/app/proxy-config.yaml")


def test_runtime_resources_match_the_existing_litellm_manifests(tmp_path: Path) -> None:
    generate_manifests(tmp_path)
    generated = {}
    for manifest in tmp_path.glob("*.yaml"):
        for resource in yaml.safe_load_all(manifest.read_text()):
            if resource is not None:
                generated[(resource["kind"], resource["metadata"]["name"])] = resource

    expected = {
        ("Deployment", "litellm"): "ducktape/cluster/k8s/litellm/app/deployment.yaml",
        ("Service", "litellm"): "ducktape/cluster/k8s/litellm/app/service.yaml",
        ("ServiceAccount", "litellm"): "ducktape/cluster/k8s/litellm/app/serviceaccount.yaml",
        ("HTTPRoute", "litellm"): "ducktape/cluster/k8s/litellm/app/httproute.yaml",
        ("ExternalSecret", "forgejo-images-creds"): "ducktape/cluster/k8s/litellm/app/forgejo-images-creds-eso.yaml",
        ("ServiceMonitor", "litellm"): "ducktape/cluster/k8s/litellm/servicemonitor/servicemonitor.yaml",
    }
    for identity, source in expected.items():
        assert generated[identity] == _committed_config(source)


if __name__ == "__main__":
    pytest_bazel.main()
