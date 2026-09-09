"""The owner-namespaced runner must resolve its source, credentials, and identity."""

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path


def _documents(path: str) -> list[dict]:
    return list(yaml.safe_load_all(get_required_path(f"_main/cluster/k8s/{path}").read_text()))


def test_cross_namespace_source_is_enabled() -> None:
    terraform = _documents("ollama/secrets/ollama-bearer-token-tf.yaml")[0]
    controller = _documents("tofu-controller/tofu-controller.yaml")[0]
    if terraform["spec"]["sourceRef"]["namespace"] != terraform["metadata"]["namespace"]:
        assert controller["spec"]["values"]["allowCrossNamespaceRefs"] is True


def test_state_password_is_reflected_to_runner_namespace() -> None:
    terraform = _documents("ollama/secrets/ollama-bearer-token-tf.yaml")[0]
    credentials = _documents("tofu-state/db/credentials.sops.yaml")[0]
    password = next(env for env in terraform["spec"]["runnerPodTemplate"]["spec"]["env"] if env["name"] == "PGPASSWORD")
    assert password["valueFrom"]["secretKeyRef"]["name"] == credentials["metadata"]["name"]
    assert password["valueFrom"]["secretKeyRef"]["key"] in credentials["stringData"]
    annotations = credentials["metadata"]["annotations"]
    for mode in ("allowed", "auto"):
        destinations = annotations[f"reflector.v1.k8s.emberstack.com/reflection-{mode}-namespaces"].split(",")
        assert terraform["metadata"]["namespace"] in destinations


def test_runner_identity_is_bound_only_in_owner_namespace() -> None:
    terraform = _documents("ollama/secrets/ollama-bearer-token-tf.yaml")[0]
    resources = _documents("ollama/secrets/tf-runner.yaml")
    account = next(resource for resource in resources if resource["kind"] == "ServiceAccount")
    binding = next(resource for resource in resources if resource["kind"] == "RoleBinding")
    for resource in resources:
        assert resource["metadata"]["namespace"] == terraform["metadata"]["namespace"]
    assert account["metadata"]["name"] == terraform["spec"]["serviceAccountName"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": account["metadata"]["name"], "namespace": account["metadata"]["namespace"]}
    ]
    # The chart also grants allowedNamespaces runner accounts cluster-wide access.
    controller = _documents("tofu-controller/tofu-controller.yaml")[0]
    allowed = controller["spec"]["values"]["runner"]["serviceAccount"].get("allowedNamespaces", [])
    assert account["metadata"]["namespace"] not in allowed


if __name__ == "__main__":
    pytest_bazel.main()
