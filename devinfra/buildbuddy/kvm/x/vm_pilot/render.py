"""Render one stopped, manual 16-GiB KVM worker from the current pool artifact."""

import argparse
from pathlib import Path

import yaml

NAME = "kvm-canary-20261109"
NAMESPACE = "buildbuddy-executor"
_LABEL = "app.kubernetes.io/part-of"
_SOURCE_NAME = "buildbuddy-kvm-worker"
_GUEST_MEMORY = "16Gi"
_EXECUTOR_MEMORY_BYTES = 15 * 1024**3
_DATA_VOLUME_NAME = f"{NAME}-work"


def render_worker(source: str) -> str:
    objects = list(yaml.safe_load_all(source))
    expected = ["ConfigMap", "VirtualMachinePool", "CiliumNetworkPolicy", "Service", "ServiceMonitor", "PrometheusRule"]
    if [obj["kind"] for obj in objects] != expected:
        raise ValueError("Unexpected worker artifact; review the renderer against its new shape")
    config, pool, policy, service, monitor, _rule = objects
    if any(obj["metadata"]["namespace"] != NAMESPACE for obj in objects):
        raise ValueError("Unexpected worker namespace")
    if pool["metadata"]["name"] != _SOURCE_NAME or pool["spec"]["replicas"] != 1:
        raise ValueError("Unexpected source pool")

    entrypoint = config["data"]["run-executor.sh"]
    pool_argument = "--env MY_POOL=kvm-vm"
    if entrypoint.count(pool_argument) != 1:
        raise ValueError("Expected one source pool argument in the executor entrypoint")
    config["data"]["run-executor.sh"] = entrypoint.replace(pool_argument, f"--env MY_POOL={NAME}")
    settings = yaml.safe_load(config["data"]["executor.yaml"])
    executor = settings["executor"]
    if (executor["millicpu"], executor["memory_bytes"], executor["max_concurrent_tasks"]) != (4000, 7 * 1024**3, 1):
        raise ValueError("Source executor sizing or exclusive-task policy changed")
    executor["memory_bytes"] = _EXECUTOR_MEMORY_BYTES
    config["data"]["executor.yaml"] = yaml.safe_dump(settings, sort_keys=False)
    config["metadata"] = {"name": NAME, "namespace": NAMESPACE, "labels": {_LABEL: NAME}}

    vm_spec = pool["spec"]["virtualMachineTemplate"]["spec"]
    if len(vm_spec["dataVolumeTemplates"]) != 1:
        raise ValueError("Expected one disposable worker disk")
    disk = vm_spec["dataVolumeTemplates"][0]
    if disk["metadata"]["name"] != "work" or disk["spec"]["storage"]["resources"]["requests"]["storage"] != "80Gi":
        raise ValueError("Expected the worker's 80-GiB disposable work disk")
    disk["metadata"]["name"] = _DATA_VOLUME_NAME
    vm_spec["runStrategy"] = "Halted"
    template = vm_spec["template"]
    template["metadata"]["labels"] = {_LABEL: NAME}
    guest = template["spec"]
    work_volumes = [volume for volume in guest["volumes"] if "dataVolume" in volume]
    if len(work_volumes) != 1 or work_volumes[0]["dataVolume"]["name"] != "work":
        raise ValueError("Expected one source work volume")
    work_volumes[0]["dataVolume"]["name"] = _DATA_VOLUME_NAME
    resources = guest["domain"]["resources"]
    if resources["requests"]["memory"] != "8Gi" or resources["limits"]["memory"] != "8Gi":
        raise ValueError("Source VM memory changed")
    resources["requests"]["memory"] = _GUEST_MEMORY
    resources["limits"]["memory"] = _GUEST_MEMORY
    config_volumes = [volume for volume in guest["volumes"] if "configMap" in volume]
    if len(config_volumes) != 1 or config_volumes[0]["configMap"]["name"] != _SOURCE_NAME:
        raise ValueError("Expected one source worker ConfigMap volume")
    config_volumes[0]["configMap"]["name"] = NAME
    vm = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": NAME,
            "namespace": NAMESPACE,
            "labels": {_LABEL: NAME},
            "annotations": {"description": "Manual, stopped single-slot pilot for the 10-GB VM system test."},
        },
        "spec": vm_spec,
    }

    policy["metadata"] = {"name": NAME, "namespace": NAMESPACE}
    if policy["spec"]["endpointSelector"]["matchLabels"] != {_LABEL: _SOURCE_NAME}:
        raise ValueError("Unexpected production network selector")
    policy["spec"]["endpointSelector"]["matchLabels"] = {_LABEL: NAME}
    service["metadata"] = {"name": f"{NAME}-metrics", "namespace": NAMESPACE, "labels": {_LABEL: NAME}}
    if service["spec"]["selector"] != {_LABEL: _SOURCE_NAME}:
        raise ValueError("Unexpected production Service selector")
    service["spec"]["selector"] = {_LABEL: NAME}
    monitor["metadata"] = {"name": NAME, "namespace": NAMESPACE}
    if monitor["spec"]["selector"]["matchLabels"] != {_LABEL: _SOURCE_NAME}:
        raise ValueError("Unexpected production ServiceMonitor selector")
    monitor["spec"]["selector"]["matchLabels"] = {_LABEL: NAME}
    return yaml.safe_dump_all([config, vm, policy, service, monitor], sort_keys=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render_worker(args.worker_manifest.read_text()))


if __name__ == "__main__":
    main()
