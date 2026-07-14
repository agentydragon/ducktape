import pytest
import pytest_bazel
import yaml

from cluster.k8s.litellm.app.generate_litellm import generate, generate_chatgpt
from util.bazel.runfiles import get_required_path


def test_litellm_yaml_matches_generator() -> None:
    for filename, generated_text in {
        "proxy-config.yaml": generate(),
        "chatgpt-proxy-config.yaml": generate_chatgpt(),
    }.items():
        committed = yaml.safe_load(get_required_path(f"ducktape/cluster/k8s/litellm/app/{filename}").read_text())
        if committed != yaml.safe_load(generated_text):
            pytest.fail(
                f"{filename} is semantically out of sync with generate_litellm.py.\n"
                "Run: bb run //cluster/k8s/litellm/app:generate_litellm_bin"
            )


def test_chatgpt_models_are_isolated_behind_internal_proxy() -> None:
    main_config = yaml.safe_load(generate())
    chatgpt_config = yaml.safe_load(generate_chatgpt())
    main_models = {
        model["model_name"]: model for model in main_config["model_list"] if model["model_name"].endswith("-chatgpt")
    }
    chatgpt_models = {model["model_name"]: model for model in chatgpt_config["model_list"]}

    assert chatgpt_models
    assert main_models.keys() == chatgpt_models.keys()
    assert all(not model["litellm_params"]["model"].startswith("chatgpt/") for model in main_config["model_list"])
    for name, main_model in main_models.items():
        assert main_model == {
            "model_name": name,
            "litellm_params": {
                "model": f"litellm_proxy/{name}",
                "api_base": "http://litellm-chatgpt.litellm.svc.cluster.local:4000/v1",
                "api_key": "os.environ/LITELLM_MASTER_KEY",
            },
            "model_info": {"mode": "responses"},
        }
        assert chatgpt_models[name] == {
            "model_name": name,
            "litellm_params": {"model": f"chatgpt/{name.removesuffix('-chatgpt')}"},
            "model_info": {"mode": "responses"},
        }


def test_chatgpt_auth_state_is_absent_from_main_deployment() -> None:
    main = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/deployment.yaml").read_text())
    chatgpt = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/chatgpt-deployment.yaml").read_text())
    main_pod = main["spec"]["template"]["spec"]
    chatgpt_pod = chatgpt["spec"]["template"]["spec"]

    assert main["spec"]["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
    assert chatgpt["spec"]["replicas"] == 1
    assert chatgpt["spec"]["strategy"] == {"type": "Recreate"}
    assert "initContainers" not in main_pod
    assert [container["name"] for container in chatgpt_pod["initContainers"]] == ["seed-chatgpt-auth"]
    assert "CHATGPT_TOKEN_DIR" not in {env["name"] for env in main_pod["containers"][0]["env"]}
    assert "CHATGPT_TOKEN_DIR" in {env["name"] for env in chatgpt_pod["containers"][0]["env"]}
    assert {volume["name"] for volume in main_pod["volumes"]}.isdisjoint({"chatgpt-auth", "chatgpt-auth-seed"})
    chatgpt_volumes = {volume["name"]: volume for volume in chatgpt_pod["volumes"]}
    assert chatgpt_volumes.keys() == {"config", "chatgpt-auth", "chatgpt-auth-seed"}
    assert chatgpt_volumes["config"]["configMap"]["name"] == "litellm-chatgpt-config"
    assert chatgpt_volumes["chatgpt-auth"]["persistentVolumeClaim"]["claimName"] == "litellm-chatgpt-auth"
    assert chatgpt_volumes["chatgpt-auth-seed"]["secret"]["secretName"] == "litellm-chatgpt-auth-seed"
    assert main["spec"]["selector"] != chatgpt["spec"]["selector"]


def test_config_maps_mount_their_matching_generated_configs() -> None:
    kustomization = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/kustomization.yaml").read_text())
    generated_files = {config["name"]: config["files"] for config in kustomization["configMapGenerator"]}
    assert generated_files == {
        "litellm-chatgpt-config": ["config.yaml=chatgpt-proxy-config.yaml"],
        "litellm-config": ["config.yaml=proxy-config.yaml"],
    }


def test_chatgpt_proxy_is_cluster_private_and_selectors_are_disjoint() -> None:
    main_service = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/service.yaml").read_text())
    chatgpt_service = yaml.safe_load(
        get_required_path("ducktape/cluster/k8s/litellm/app/chatgpt-service.yaml").read_text()
    )
    route = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/httproute.yaml").read_text())

    assert main_service["spec"]["type"] == "ClusterIP"
    assert chatgpt_service["spec"]["type"] == "ClusterIP"
    assert main_service["spec"]["selector"] == {"app.kubernetes.io/name": "litellm"}
    assert chatgpt_service["spec"]["selector"] == {"app.kubernetes.io/name": "litellm-chatgpt"}
    assert {backend["name"] for rule in route["spec"]["rules"] for backend in rule["backendRefs"]} == {"litellm"}


if __name__ == "__main__":
    pytest_bazel.main()
