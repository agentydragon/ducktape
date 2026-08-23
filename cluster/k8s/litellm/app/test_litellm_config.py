"""Cross-artifact contracts for the committed main LiteLLM config."""

from pathlib import PurePosixPath

import pytest_bazel
import yaml
from more_itertools import one

from cluster.validation.terraform_hcl import locals_blocks
from util.bazel.runfiles import get_required_path


def _load_yaml(filename: str) -> dict:
    loaded = yaml.safe_load(get_required_path(f"ducktape/cluster/k8s/litellm/app/{filename}").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _litellm_keys_locals() -> dict:
    blocks = locals_blocks(get_required_path("ducktape/tf/gitops/litellm-keys/main.tf"))
    return one(blocks)


def test_terraform_codex_lanes_match_the_cliproxy_routes() -> None:
    """Both client protocols must expose the same CLIProxyAPI downstream models."""
    routes = {entry["model_name"]: entry for entry in _load_yaml("proxy-config.yaml")["model_list"]}
    tf_locals = _litellm_keys_locals()
    responses_names = tf_locals["oai_lane_models"]
    messages_names = tf_locals["codex_client_models"]

    assert set(responses_names) <= routes.keys()
    assert set(messages_names) <= routes.keys()

    def downstream_model(name: str, provider: str) -> str:
        route = routes[name]["litellm_params"]
        model = route["model"]
        assert isinstance(model, str)
        prefix, downstream = model.split("/", 1)
        assert prefix == provider
        return downstream

    responses_by_downstream = {downstream_model(name, "openai"): routes[name] for name in responses_names}
    messages_by_downstream = {downstream_model(name, "anthropic"): routes[name] for name in messages_names}
    assert responses_by_downstream.keys() == messages_by_downstream.keys()

    for downstream, responses_route in responses_by_downstream.items():
        messages_route = messages_by_downstream[downstream]
        responses_params = responses_route["litellm_params"]
        messages_params = messages_route["litellm_params"]
        assert responses_params["api_base"] == f"{messages_params['api_base']}/v1"
        assert responses_params["api_key"] == messages_params["api_key"]


def test_terraform_key_allowlists_only_name_models_the_proxy_serves() -> None:
    served = {entry["model_name"] for entry in _load_yaml("proxy-config.yaml")["model_list"]}
    model_locals = {
        name: models
        for name, models in _litellm_keys_locals().items()
        if name.endswith("_models") and isinstance(models, list)
    }
    assert model_locals, "expected Terraform to declare at least one model allowlist"

    for local, models in model_locals.items():
        missing = [model for model in models if model not in served]
        assert not missing, f"{local} allows models the proxy does not serve: {missing}"


def test_kustomize_config_sources_are_mounted_at_the_configured_path() -> None:
    kustomization = _load_yaml("kustomization.yaml")
    deployment = _load_yaml("deployment.yaml")
    pod_spec = deployment["spec"]["template"]["spec"]

    for generator in kustomization["configMapGenerator"]:
        volume = one(
            candidate
            for candidate in pod_spec["volumes"]
            if candidate.get("configMap", {}).get("name") == generator["name"]
        )
        for assignment in generator["files"]:
            key, source = assignment.split("=", 1)
            assert get_required_path(f"ducktape/cluster/k8s/litellm/app/{source}").is_file()

            projected = one(item for item in volume["configMap"]["items"] if item["key"] == key)
            container = one(
                candidate
                for candidate in pod_spec["containers"]
                if any(mount["name"] == volume["name"] for mount in candidate.get("volumeMounts", []))
            )
            mount = one(mount for mount in container["volumeMounts"] if mount["name"] == volume["name"])
            configured_path = str(PurePosixPath(mount["mountPath"]) / projected["path"])
            assert configured_path in container["args"]


if __name__ == "__main__":
    pytest_bazel.main()
