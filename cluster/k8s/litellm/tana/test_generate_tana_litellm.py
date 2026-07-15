import pytest
import pytest_bazel
import yaml

from cluster.k8s.litellm.tana.generate_tana_litellm import generate
from tana.litellm_proxy.model_registry import TANA_LLM_PROXY_RESPONDING_MODELS
from util.bazel.runfiles import get_required_path


def test_tana_litellm_yaml_matches_generator() -> None:
    committed_text = get_required_path("ducktape/cluster/k8s/litellm/tana/proxy-config.yaml").read_text()
    committed = yaml.safe_load(committed_text)
    generated = yaml.safe_load(generate())
    if committed != generated:
        pytest.fail(
            "proxy-config.yaml is semantically out of sync with generate_tana_litellm.py.\n"
            "Run: bazel run //cluster/k8s/litellm/tana:generate_tana_litellm_bin"
        )


def test_tana_litellm_exposes_every_responding_model_once() -> None:
    config = yaml.safe_load(generate())
    configured = [entry["model_name"] for entry in config["model_list"]]
    expected = [model.model_id for model in TANA_LLM_PROXY_RESPONDING_MODELS]
    assert configured == expected
    assert len(configured) == len(set(configured))
    assert [entry["litellm_params"]["model"] for entry in config["model_list"]] == [
        f"tana/tana/{model.model_id}" for model in TANA_LLM_PROXY_RESPONDING_MODELS
    ]
    assert all(entry["litellm_params"]["custom_llm_provider"] == "tana" for entry in config["model_list"])


def test_custom_handler_config_uses_adjacent_import_shim() -> None:
    config = yaml.safe_load(generate())
    assert "custom_provider_map" not in config
    assert config["litellm_settings"]["custom_provider_map"] == [
        {"provider": "tana", "custom_handler": "custom_handler.tana_handler"}
    ]
    shim = get_required_path("ducktape/cluster/k8s/litellm/tana/custom_handler.py").read_text()
    assert "from tana.litellm_proxy.custom_handler import tana_handler" in shim


if __name__ == "__main__":
    pytest_bazel.main()
