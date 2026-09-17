"""Pinning tests for the cdk8s LiteLLM manifest generator.

`test_generated_manifests_match_committed` is the "LiteLLM config pattern"
generated-output snapshot (STYLE.md § Testing), generalized from the ConfigMap
payload to the whole generated file set: the committed files under
cluster/k8s/litellm/app are the source of truth, and this test proves
regeneration reproduces them exactly.
"""

from pathlib import Path

import pytest_bazel
import yaml

from cluster.generate_manifests import generate_manifests
from cluster.litellm_config import main_proxy_config
from util.bazel.runfiles import get_required_path

_GENERATED_FILES = (
    "cluster/k8s/litellm/app/litellm.k8s.yaml",
    "cluster/k8s/litellm/app/flux-kustomization.yaml",
    "cluster/k8s/litellm/app/kustomization.yaml",
)


def _committed_config(path: str) -> dict:
    config = yaml.safe_load(get_required_path(path).read_text())
    assert isinstance(config, dict)
    return config


def test_main_proxy_config_is_generated_from_the_roster() -> None:
    assert main_proxy_config() == _committed_config("ducktape/cluster/k8s/litellm/app/proxy-config.yaml")


def test_generated_manifests_match_committed(tmp_path: Path) -> None:
    """Regenerate with `bb run //cluster:generate_manifests` and commit the result if this fails."""
    generate_manifests(tmp_path)
    for relative in _GENERATED_FILES:
        generated = (tmp_path / relative).read_text()
        committed = get_required_path(f"ducktape/{relative}").read_text()
        assert generated == committed, f"{relative} is stale"
        # cdk8s can't emit YAML comments, so this should be unreachable -- but if it
        # ever did, Flux's image-automation bot would silently fight the generator
        # for ownership of this file (cluster/docs/cdk8s.md).
        assert "$imagepolicy" not in generated, f"{relative} must not carry a Flux image-automation marker"


if __name__ == "__main__":
    pytest_bazel.main()
