import pytest
import pytest_bazel
import yaml

from devinfra.buildbuddy.kvm.x.shared_cache_canary.render import (
    CANARY,
    HOSTNAME,
    LABEL,
    NAMESPACE,
    POOL,
    PROXY,
    production_manifest,
    proxy_manifest,
    render,
)

_HOSTS = ("host-a", "host-b")


@pytest.mark.parametrize("shared", [False, True])
def test_canary_renders_isolated_workers_and_one_shared_endpoint(shared: bool) -> None:
    original = production_manifest()
    before = {
        resource["kind"]: resource for resource in yaml.safe_load_all(original) if resource["kind"] == "ConfigMap"
    }
    manifests = {
        (resource["kind"], resource["metadata"]["name"]): resource
        for resource in yaml.safe_load_all(render(original, proxy_manifest(), _HOSTS, shared=shared))
    }
    assert set(manifests) == {
        ("ConfigMap", CANARY),
        ("VirtualMachinePool", CANARY),
        ("CiliumNetworkPolicy", CANARY),
        ("ConfigMap", PROXY),
        ("Deployment", PROXY),
        ("Service", PROXY),
        ("CiliumNetworkPolicy", PROXY),
    }
    assert all(resource["metadata"]["namespace"] == NAMESPACE for resource in manifests.values())

    config = manifests["ConfigMap", CANARY]["data"]
    assert config["pool"].strip() == POOL
    executor = yaml.safe_load(config["executor.yaml"])
    old_executor = yaml.safe_load(before["ConfigMap"]["data"]["executor.yaml"])
    target = executor["executor"].pop("cache_target")
    old_executor["executor"].pop("cache_target", None)
    assert executor == old_executor
    assert target == (
        f"grpc://{PROXY}.{NAMESPACE}.svc.cluster.local:1985" if shared else executor["executor"]["app_target"]
    )
    assert config["registries.yaml"] == before["ConfigMap"]["data"]["registries.yaml"]

    pool = manifests["VirtualMachinePool", CANARY]["spec"]
    assert pool["replicas"] == 0
    labels = {LABEL: CANARY}
    assert pool["selector"]["matchLabels"] == labels
    vm = pool["virtualMachineTemplate"]["spec"]
    assert vm["template"]["metadata"]["labels"] == labels
    guest = vm["template"]["spec"]
    assert {v["name"]: v["configMap"]["name"] for v in guest["volumes"] if "configMap" in v} == {
        "worker-config": CANARY
    }
    assert {v["name"]: v["dataVolume"]["name"] for v in guest["volumes"] if "dataVolume" in v} == {
        "work": f"{CANARY}-work"
    }
    assert [t["metadata"]["name"] for t in vm["dataVolumeTemplates"]] == [f"{CANARY}-work"]
    affinity = guest["affinity"]
    for term in affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]:
        assert {"key": HOSTNAME, "operator": "In", "values": list(_HOSTS)} in term["matchExpressions"]
    assert affinity["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"] == [
        {"topologyKey": HOSTNAME, "labelSelector": {"matchLabels": labels}}
    ]

    workers = manifests["CiliumNetworkPolicy", CANARY]["spec"]
    proxy = manifests["CiliumNetworkPolicy", PROXY]["spec"]
    peer = proxy["endpointSelector"]
    assert workers["endpointSelector"]["matchLabels"] == labels
    assert (
        {"toEndpoints": [peer], "toPorts": [{"ports": [{"port": "1985", "protocol": "TCP"}]}]} in workers["egress"]
    ) == shared
    assert proxy["ingress"][0]["fromEndpoints"] == [{"matchLabels": labels}]
    deployment = manifests["Deployment", PROXY]["spec"]
    assert deployment["replicas"] == 1
    assert deployment["template"]["spec"]["nodeSelector"] == {HOSTNAME: _HOSTS[0]}
    service = manifests["Service", PROXY]["spec"]
    assert service["selector"] == peer["matchLabels"]
    assert service.get("internalTrafficPolicy", "Cluster") == "Cluster"


def test_canary_requires_distinct_hosts() -> None:
    with pytest.raises(ValueError, match="two distinct"):
        render("", "", ("host-a", "host-a"), shared=False)


if __name__ == "__main__":
    pytest_bazel.main()
