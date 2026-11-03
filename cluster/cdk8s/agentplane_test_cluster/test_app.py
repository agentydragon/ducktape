from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s.agentplane_test_cluster import app as test_cluster


@pytest.fixture(scope="module")
def objects(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(test_cluster.chart(scratch_app_factory(), image="node:test")))


def _one(objects: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return one(obj for obj in objects if obj["kind"] == kind)


def test_privileged_pod_is_scoped_to_its_namespace(objects: list[dict[str, Any]]) -> None:
    namespace = _one(objects, "Namespace")
    labels = namespace["metadata"]["labels"]

    assert namespace["metadata"]["name"] == test_cluster.NAME
    assert {
        key: labels[key]
        for key in (
            "pod-security.kubernetes.io/enforce",
            "pod-security.kubernetes.io/audit",
            "pod-security.kubernetes.io/warn",
        )
    } == {
        "pod-security.kubernetes.io/enforce": "privileged",
        "pod-security.kubernetes.io/audit": "privileged",
        "pod-security.kubernetes.io/warn": "privileged",
    }


def test_privileged_node_stays_inside_its_outer_pod(objects: list[dict[str, Any]]) -> None:
    deployment = _one(objects, "Deployment")
    pod = deployment["spec"]["template"]["spec"]

    assert pod.get("hostNetwork") is not True
    assert pod.get("hostPID") is not True
    assert pod.get("hostIPC") is not True
    assert pod["automountServiceAccountToken"] is False
    assert pod["imagePullSecrets"] == [{"name": test_cluster.SECRET_NAME}]
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "wyrm2"}
    assert all("hostPath" not in volume for volume in pod["volumes"])
    assert all("secret" not in volume for volume in pod["volumes"])
    assert len(pod["containers"]) == 1
    container = pod["containers"][0]
    assert str(container["resources"]["requests"]["devices.kubevirt.io/kvm"]) == "1"
    assert str(container["resources"]["limits"]["devices.kubevirt.io/kvm"]) == "1"
    assert all("hostPort" not in port for port in container.get("ports", []))
    assert container["securityContext"]["privileged"] is True
    # The image supplies the trusted entrypoint; keeping command and args absent avoids
    # turning the chart into a path for injecting arbitrary commands into the node.
    assert "command" not in container
    assert "args" not in container


def test_nested_state_uses_only_bounded_persistent_storage(objects: list[dict[str, Any]]) -> None:
    pvc = _one(objects, "PersistentVolumeClaim")
    deployment = _one(objects, "Deployment")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert pvc["spec"]["storageClassName"] == "piraeus-ssd-local"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "80Gi"
    data_volume = one(volume for volume in pod["volumes"] if volume["name"] == "data")
    assert data_volume["persistentVolumeClaim"]["claimName"] == pvc["metadata"]["name"]
    assert {
        (mount["mountPath"], mount.get("subPath")) for mount in container["volumeMounts"] if mount["name"] == "data"
    } == {
        ("/var/lib/rancher/k3s", "data"),
        ("/var/lib/kubelet", "kubelet"),
        ("/var/log", "logs"),
        ("/var/lib/cni", "cni"),
    }


def test_network_policy_limits_outer_node_traffic(objects: list[dict[str, Any]]) -> None:
    policy = _one(objects, "CiliumNetworkPolicy")["spec"]
    service = _one(objects, "Service")["spec"]

    assert service["type"] == "ClusterIP"
    assert len(service["ports"]) == 1
    assert len(policy["ingress"]) == 1
    ingress = one(policy["ingress"])
    assert set(ingress) <= {"fromEndpoints", "toPorts"}
    assert ingress["fromEndpoints"] == [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "buildbuddy-executor"}}]
    assert ingress["toPorts"][0]["ports"][0]["port"] == str(service["ports"][0]["targetPort"])

    egress_rules = {}
    for rule in policy["egress"]:
        assert set(rule) <= {"toEndpoints", "toPorts"}
        assert not {"toEntities", "toCIDR", "toCIDRSet", "toFQDNs"}.intersection(rule)
        assert len(rule["toEndpoints"]) == 1
        peer = frozenset(rule["toEndpoints"][0]["matchLabels"].items())
        ports = frozenset(
            (port["port"], port["protocol"]) for port_rule in rule["toPorts"] for port in port_rule["ports"]
        )
        assert peer not in egress_rules
        egress_rules[peer] = ports

    assert egress_rules == {
        frozenset({"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}.items()): frozenset(
            {("53", "UDP"), ("53", "TCP")}
        ),
        frozenset(test_cluster.zot.HTTP.pods.cilium.items()): frozenset({(str(test_cluster.zot.HTTP.pod_port), "TCP")}),
    }


def test_registry_config_has_only_in_cluster_cache_endpoints(objects: list[dict[str, Any]]) -> None:
    config_map = _one(objects, "ConfigMap")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    registries = yaml.safe_load(config_map["data"]["registries.yaml"])

    assert config["disable-default-registry-endpoint"] is True
    assert registries["mirrors"]
    assert all(
        endpoint == f"http://{test_cluster.zot.HTTP.fqdn}"
        for mirror in registries["mirrors"].values()
        for endpoint in mirror["endpoint"]
    )


def test_private_image_pull_secret_uses_the_scoped_registry_store(objects: list[dict[str, Any]]) -> None:
    external_secret = _one(objects, "ExternalSecret")

    assert external_secret["metadata"]["name"] == test_cluster.SECRET_NAME
    assert external_secret["metadata"]["namespace"] == test_cluster.NAME
    assert external_secret["spec"]["secretStoreRef"] == {
        "kind": "ClusterSecretStore",
        "name": "kubernetes-forgejo-images-secret-store",
    }


def test_inner_security_seed_is_config_data_not_an_outer_resource(objects: list[dict[str, Any]]) -> None:
    config_map = _one(objects, "ConfigMap")
    seed = list(yaml.safe_load_all(config_map["data"]["00-shared-security.yaml"]))

    assert seed
    assert all(obj["kind"] != "Namespace" for obj in seed)
    seed_identities = {(obj["kind"], obj["metadata"]["name"], obj["metadata"].get("namespace")) for obj in seed}
    outer_identities = {(obj["kind"], obj["metadata"]["name"], obj["metadata"].get("namespace")) for obj in objects}
    assert seed_identities.isdisjoint(outer_identities)
    deployment = _one(objects, "Deployment")
    assert any(
        mount["mountPath"] == "/var/lib/rancher/k3s/server/manifests/00-shared-security.yaml"
        and mount["subPath"] == "00-shared-security.yaml"
        for mount in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    )


if __name__ == "__main__":
    pytest_bazel.main()
