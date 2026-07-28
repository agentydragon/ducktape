import pytest_bazel
import yaml

from cluster.k8s.litellm.app.generate_litellm import OPENCLAW_CODEX_MODELS, generate
from util.bazel.runfiles import get_required_path


def test_openclaw_models_match_litellm_codex_routes() -> None:
    """Keep OpenClaw's static catalog aligned with the generated LiteLLM routes."""
    openclaw = yaml.safe_load(
        get_required_path("ducktape/cluster/k8s/agents/openclaw/gateway/openclawinstance.yaml").read_text()
    )
    provider = openclaw["spec"]["config"]["raw"]["models"]["providers"]["litellm-subscription"]
    configured_ids = [model["id"] for model in provider["models"]]

    assert configured_ids == OPENCLAW_CODEX_MODELS
    assert provider["api"] == "anthropic-messages"
    assert openclaw["spec"]["config"]["raw"]["agents"]["defaults"]["model"]["primary"] in {
        f"litellm-subscription/{model_id}" for model_id in OPENCLAW_CODEX_MODELS
    }

    litellm_models = {entry["model_name"]: entry for entry in yaml.safe_load(generate())["model_list"]}
    for model_id in OPENCLAW_CODEX_MODELS:
        assert litellm_models[model_id] == {
            "model_name": model_id,
            "litellm_params": {
                "model": f"anthropic/{model_id.removeprefix('codex-')}",
                "api_base": "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317",
                "api_key": "os.environ/CLIPROXY_CLIENT_KEY",
            },
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


if __name__ == "__main__":
    pytest_bazel.main()
