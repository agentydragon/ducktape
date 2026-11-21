"""Render the manual, two-host KVM cache trial from the checked-in worker manifest."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from python.runfiles import Runfiles

WORKER = "buildbuddy-kvm-worker"
CANARY = "buildbuddy-kvm-cache-canary"
POOL = "kvm-vm-cache-canary"
PROXY = "buildbuddy-kvm-cache-proxy"
LABEL = "app.kubernetes.io/part-of"
HOSTNAME = "kubernetes.io/hostname"
GRPC_PORT = 1985
NAMESPACE = "buildbuddy-executor"
_RUNFILES_ROOT = "ducktape/devinfra/buildbuddy/kvm/x/shared_cache_canary"


def production_manifest() -> str:
    return (
        Runfiles.CreateOrRaise().root() / "ducktape/cluster/k8s/buildbuddy-kvm-worker/buildbuddy-kvm-worker.k8s.yaml"
    ).read_text()


def proxy_manifest() -> str:
    return (Runfiles.CreateOrRaise().root() / _RUNFILES_ROOT / "proxy.yaml").read_text()


def render(worker_yaml: str, proxy_yaml: str, hosts: tuple[str, str], *, shared: bool) -> str:
    if len(set(hosts)) != 2 or not all(hosts):
        raise ValueError("choose two distinct physical host labels")

    source = {
        resource["kind"]: resource
        for resource in yaml.safe_load_all(worker_yaml)
        if resource["metadata"].get("name") == WORKER
    }
    config, pool, policy = (source[kind] for kind in ("ConfigMap", "VirtualMachinePool", "CiliumNetworkPolicy"))
    for resource in (config, pool, policy):
        if resource["metadata"]["namespace"] != NAMESPACE:
            raise ValueError("the worker's existing Secret references require the buildbuddy-executor namespace")
        resource["metadata"]["name"] = CANARY

    config["data"]["pool"] = POOL + "\n"
    executor_config = yaml.safe_load(config["data"]["executor.yaml"])
    executor = executor_config["executor"]
    if shared:
        executor["cache_target"] = f"grpc://{PROXY}.{NAMESPACE}.svc.cluster.local:{GRPC_PORT}"
    else:
        executor["cache_target"] = executor["app_target"]
    config["data"]["executor.yaml"] = yaml.safe_dump(executor_config, sort_keys=False)

    worker_labels = {LABEL: CANARY}
    pool["metadata"]["labels"] = worker_labels
    pool_spec = pool["spec"]
    pool_spec["replicas"] = 0
    pool_spec["selector"]["matchLabels"] = worker_labels
    vm = pool_spec["virtualMachineTemplate"]
    vm["metadata"]["labels"] = worker_labels
    vmi = vm["spec"]["template"]
    vmi["metadata"]["labels"] = worker_labels
    vm_spec = vmi["spec"]
    affinity = vm_spec["affinity"]
    for term in affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]:
        term["matchExpressions"].append({"key": HOSTNAME, "operator": "In", "values": list(hosts)})
    affinity["podAntiAffinity"] = {
        "requiredDuringSchedulingIgnoredDuringExecution": [
            {"topologyKey": HOSTNAME, "labelSelector": {"matchLabels": worker_labels}}
        ]
    }
    # CDI derives PVC names from the template name and pool index. Preserve the
    # guest's virtio disk name and serial while allocating canary-only volumes.
    for volume in vm_spec["volumes"]:
        if "configMap" in volume:
            volume["configMap"]["name"] = CANARY
        if "dataVolume" in volume:
            volume["dataVolume"]["name"] = f"{CANARY}-{volume['dataVolume']['name']}"
    for template in vm["spec"]["dataVolumeTemplates"]:
        template["metadata"]["name"] = f"{CANARY}-{template['metadata']['name']}"

    policy["spec"]["endpointSelector"]["matchLabels"] = worker_labels
    if shared:
        policy["spec"]["egress"].append(
            {
                "toEndpoints": [{"matchLabels": {"app.kubernetes.io/name": PROXY}}],
                "toPorts": [{"ports": [{"port": str(GRPC_PORT), "protocol": "TCP"}]}],
            }
        )
    proxies = list(yaml.safe_load_all(proxy_yaml))
    deployment = next(resource for resource in proxies if resource["kind"] == "Deployment")
    deployment["spec"]["template"]["spec"]["nodeSelector"] = {HOSTNAME: hosts[0]}
    return yaml.safe_dump_all([config, pool, policy, *proxies], sort_keys=False, explicit_start=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=("direct", "shared"), required=True)
    parser.add_argument("--hosts", nargs=2, metavar=("PROXY_HOST", "SECOND_HOST"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    first_host, second_host = args.hosts
    args.output.write_text(
        render(production_manifest(), proxy_manifest(), (first_host, second_host), shared=args.route == "shared")
    )


if __name__ == "__main__":
    main()
