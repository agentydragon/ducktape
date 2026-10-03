"""Production routing/authority cutover must not leave a direct-runner fallback."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from cdk8s import Chart, Testing as CdkTesting
from more_itertools import one

from agentplane.sandbox_service.kubevirt import VmTemplate
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane import app, notifications, sandbox_service, staging
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
    kubevirt_rules = [rule for rule in app_rules if rule.get("apiGroups") == ["kubevirt.io"]]
    assert {resource for rule in kubevirt_rules for resource in rule["resources"]} == {
        "virtualmachines",
        "virtualmachineinstances",
    }
    assert all(set(rule["verbs"]) == {"get", "list", "watch"} for rule in kubevirt_rules)
    backend_config = yaml.safe_load(resource("ConfigMap", f"{sandbox_service.NAME}-config")["data"]["config.yaml"])
    assert backend_config["kubernetes_binding_cleanup_namespaces"] == sorted(
        backend_config["kubernetes_binding_cleanup_namespaces"]
    )
    assert backend_config["caller_accounts"] == [
        {"namespace": namespace, "name": app.NAME},
        {"namespace": namespace, "name": notifications.NAME},
    ]
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


def test_vm_catalog_emits_admission_and_launcher_fence_together() -> None:
    namespace = staging.ENV.namespace
    template = VmTemplate(
        image=f"registry.test/guest@sha256:{'a' * 64}",
        image_pull_secret="test-pull-creds",
        storage_class="test-local-storage",
        llm_base_url="http://llm.test",
        proxy_url="http://10.0.2.2:3128",
        ca_bundle="PUBLIC CA",
        kubernetes_host="kubernetes.test",
        kubernetes_credential_name="test-credential",
    )
    chart = Chart(CdkTesting.app(), "vm-catalog", disable_resource_name_hashes=True)
    sandbox_service.SandboxService(
        chart,
        "sandbox-service",
        staging.ENV,
        manager=ServiceAccountRef(namespace=namespace, name=app.NAME),
        caller=app.service(namespace),
        vm_templates={"test-vm": template},
        vm_relay_image=f"registry.test/relay@sha256:{'b' * 64}",
    )
    documents = CdkTesting.synth(chart)
    config = one(
        item
        for item in documents
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "agentplane-sandbox-service-config"
    )
    assert set(yaml.safe_load(config["data"]["config.yaml"])["vm_templates"]) == {"test-vm"}
    policy = one(item for item in documents if item["kind"] == "ClusterPolicy")
    assert policy["metadata"]["name"] == f"{namespace}-launcher-relay"
    fence = one(
        item
        for item in documents
        if item["kind"] == "CiliumNetworkPolicy" and item["metadata"]["name"] == "agentplane-vm-launchers"
    )
    assert fence["spec"]["endpointSelector"]["matchLabels"] == {
        "kubevirt.io": "virt-launcher",
        "agentplane.allegedly.works/managed": "true",
    }
    assert fence["spec"]["ingress"] == [
        {
            "fromEndpoints": [{"matchLabels": sandbox_service.service(namespace).pods.cilium}],
            "toPorts": [{"ports": [{"port": "7000", "protocol": "TCP"}]}],
        }
    ]


def test_vm_catalog_requires_pinned_relay_image() -> None:
    chart = Chart(CdkTesting.app(), "missing-relay", disable_resource_name_hashes=True)
    with pytest.raises(ValueError, match="digest-pinned relay image"):
        sandbox_service.SandboxService(
            chart,
            "sandbox-service",
            staging.ENV,
            manager=ServiceAccountRef(namespace=staging.ENV.namespace, name=app.NAME),
            caller=app.service(staging.ENV.namespace),
            vm_templates={
                "test-vm": VmTemplate(
                    image=f"registry.test/guest@sha256:{'a' * 64}",
                    image_pull_secret="test-pull-creds",
                    storage_class="test-local-storage",
                    llm_base_url="http://llm.test",
                    proxy_url="http://10.0.2.2:3128",
                    ca_bundle="PUBLIC CA",
                    kubernetes_host="kubernetes.test",
                    kubernetes_credential_name="test-credential",
                )
            },
        )


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_notifications_have_no_app_or_direct_runner_dependency(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    def resource(kind: str, name: str) -> dict[str, Any]:
        return one(
            doc for doc in agentplane_manifests[namespace] if doc["kind"] == kind and doc["metadata"]["name"] == name
        )

    pod = resource("Deployment", notifications.NAME)["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == notifications.NAME
    tokens = one(volume for volume in pod["volumes"] if volume["name"] == "service-tokens")
    assert {source["serviceAccountToken"]["audience"] for source in tokens["projected"]["sources"]} == {
        "agentplane-egress",
        sandbox_service.TOKEN_AUDIENCE,
    }
    for container in pod["containers"] + pod["initContainers"]:
        database = one(item for item in container["env"] if item["name"] == "AGENTPLANE_NOTIFICATIONS_DATABASE_URL")
        assert database["valueFrom"]["secretKeyRef"] == {"name": "postgres-notifications", "key": "uri"}
    role = resource("ClusterRole", f"{namespace}-notifications-token-reviewer")
    assert role["rules"] == [
        {"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]}
    ]
    policy = resource("CiliumNetworkPolicy", notifications.NAME)["spec"]
    assert not any(
        port["port"] == "7000"
        for rule in policy["egress"]
        for ports in rule.get("toPorts", [])
        for port in ports["ports"]
    )
    assert not any(
        target["matchLabels"].get("k8s:app.kubernetes.io/name") == app.NAME
        for rule in policy["egress"]
        for target in rule.get("toEndpoints", [])
    )


if __name__ == "__main__":
    pytest_bazel.main()
