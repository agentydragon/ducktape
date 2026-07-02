import pytest
import pytest_bazel
import yaml

from cluster.k8s.haku.dispatch.litellm.generate_workers_litellm import generate, zai_zone_model_names
from util.bazel.runfiles import get_required_path


def test_workers_config_matches_generator() -> None:
    committed = yaml.safe_load(
        get_required_path("ducktape/cluster/k8s/haku/dispatch/litellm/workers-litellm-config.yaml").read_text()
    )
    if committed != yaml.safe_load(generate()):
        pytest.fail(
            "workers-litellm-config.yaml is semantically out of sync with its generator.\n"
            "Run: bazel run //cluster/k8s/haku/dispatch/litellm:generate_workers_litellm_bin"
        )


def test_zone_key_allowlist_matches_tf() -> None:
    """The zai zone key's model allowlist (tf/gitops/litellm-keys) must cover
    exactly the models the workers-LiteLLM chains — drift here silently strands
    models (in config but unusable) or over-grants (in the key but unserved)."""
    tf_text = get_required_path("ducktape/tf/gitops/litellm-keys/main.tf").read_text()
    for name in zai_zone_model_names():
        base = name.removesuffix("-anthropic")
        assert f'"{base}"' in tf_text, f"model {base!r} missing from tf/gitops/litellm-keys zai allowlist"


if __name__ == "__main__":
    pytest_bazel.main()
