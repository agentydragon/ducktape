"""Production routing/authority cutover must not leave a direct-runner fallback."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import app, sandbox_service
from cluster.cdk8s.agentplane.conftest import NAMESPACES


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_app_uses_independent_service(namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]) -> None:
    documents = agentplane_manifests[namespace]

    def resource(kind: str, name: str) -> dict[str, Any]:
        return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)

    application = resource("Deployment", app.NAME)["spec"]["template"]["spec"]
    container = one(item for item in application["containers"] if item["name"] == "app")
    target = sandbox_service.service(namespace)
    assert f"--sandbox-service-target={target.fqdn}:{target.pod_port}" in container["args"]
    assert not any(arg.startswith("--runner-port") for arg in container["args"])
    token = one(volume for volume in application["volumes"] if volume["name"] == "sandbox-service-token")
    assert token["projected"]["sources"] == [
        {
            "serviceAccountToken": {
                "audience": sandbox_service.TOKEN_AUDIENCE,
                "expirationSeconds": 3600,
                "path": "token",
            }
        }
    ]
    assert one(mount for mount in container["volumeMounts"] if mount["name"] == "sandbox-service-token")["readOnly"]
    service_pod = resource("Deployment", sandbox_service.NAME)["spec"]["template"]["spec"]
    assert service_pod["serviceAccountName"] == sandbox_service.NAME
    assert all("persistentVolumeClaim" not in volume for volume in service_pod.get("volumes", []))
    assert not service_pod.get("initContainers"), (
        "the independent service must not migrate or depend on the app database"
    )
    for rule in resource("Role", app.NAME)["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}
    app_rules = resource("Role", app.NAME)["rules"]
    kubevirt_rules = [
        rule for rule in app_rules if set(rule["resources"]) == {"virtualmachines", "virtualmachineinstances"}
    ]
    assert len(kubevirt_rules) == 1
    assert kubevirt_rules[0]["apiGroups"] == ["kubevirt.io"]
    assert set(kubevirt_rules[0]["verbs"]) == {"get", "list", "watch"}
    backend_config = yaml.safe_load(resource("ConfigMap", f"{sandbox_service.NAME}-config")["data"]["config.yaml"])
    assert backend_config["kubernetes_binding_cleanup_namespaces"] == sorted(
        backend_config["kubernetes_binding_cleanup_namespaces"]
    )
    assert backend_config["caller_accounts"] == [{"namespace": namespace, "name": app.NAME}]
    runner_policy = resource("CiliumNetworkPolicy", "agentplane-runner")["spec"]
    assert runner_policy["ingress"] == [
        {
            "fromEndpoints": [{"matchLabels": target.pods.cilium}],
            "toPorts": [{"ports": [{"port": "7000", "protocol": "TCP"}]}],
        }
    ]
    application_egress = resource("CiliumNetworkPolicy", app.NAME)["spec"]["egress"]
    assert not any(
        port["port"] == "7000"
        for rule in application_egress
        for ports in rule.get("toPorts", [])
        for port in ports["ports"]
    )


if __name__ == "__main__":
    pytest_bazel.main()
