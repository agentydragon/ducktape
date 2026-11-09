"""Render one stopped, opt-in worker from the committed production artifact."""

import argparse
import re
from pathlib import Path

import yaml

NAME = "kvm-canary-20261109"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-manifest", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--cpu", type=int, choices=(4, 8), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    objects = list(yaml.safe_load_all(args.worker_manifest.read_text()))
    config, vm, policy = objects
    if [obj["kind"] for obj in objects] != ["ConfigMap", "VirtualMachine", "CiliumNetworkPolicy"]:
        raise ValueError("Unexpected worker artifact; review the renderer against its new shape")
    original = vm["metadata"]["name"]
    for obj in objects:
        obj["metadata"]["name"] = NAME
        obj["metadata"]["labels"] = {"app.kubernetes.io/name": NAME}
    data = config["data"]
    for old, new in [(f"MY_HOSTNAME={original}", f"MY_HOSTNAME={NAME}"), ("MY_POOL=kvm-vm", f"MY_POOL={NAME}")]:
        if data["run-executor.sh"].count(old) != 1:
            raise ValueError(f"Expected one {old} in worker entrypoint")
        data["run-executor.sh"] = data["run-executor.sh"].replace(old, new)
    settings = yaml.safe_load(data["executor.yaml"])
    settings["executor"]["millicpu"] = args.cpu * 1000
    if settings["executor"]["max_concurrent_tasks"] != 1:
        raise ValueError("The canary requires one action slot")
    data["executor.yaml"] = yaml.safe_dump(settings, sort_keys=False)
    template = vm["spec"]["template"]
    vm["spec"]["runStrategy"] = "Halted"
    vm["metadata"]["annotations"] = {
        "description": "Disposable single-slot CPU/storage experiment; manually owned and outside Flux."
    }
    template["metadata"]["labels"] = {"app.kubernetes.io/name": NAME, "kubevirt.io/domain": NAME}
    spec = template["spec"]
    spec["nodeSelector"] = {"kubernetes.io/hostname": args.node}
    spec["domain"]["cpu"]["cores"] = args.cpu
    for resources in spec["domain"]["resources"].values():
        resources["cpu"] = str(args.cpu)
        if resources["memory"] != "16Gi":
            raise ValueError("The canary requires 16Gi guest memory")
    for volume in spec["volumes"]:
        if "configMap" in volume:
            volume["configMap"]["name"] = NAME
        if "emptyDisk" in volume and volume["emptyDisk"]["capacity"] != "80Gi":
            raise ValueError("The canary requires 80Gi disposable scratch")
        if "cloudInitNoCloud" in volume:
            cloud_init = yaml.safe_load(volume["cloudInitNoCloud"]["userData"])
            cloud_init["packages"].append("sysstat")
            volume["cloudInitNoCloud"]["userData"] = "#cloud-config\n" + yaml.safe_dump(cloud_init, sort_keys=False)
    policy["spec"]["endpointSelector"]["matchLabels"]["kubevirt.io/domain"] = NAME
    result = yaml.safe_dump_all(objects, sort_keys=False)
    # The source service/container paths are guest-local and intentionally shared
    # with production. Any remaining outer domain selector would affect production.
    if re.search(r"kubevirt.io/domain: " + re.escape(original), result):
        raise ValueError("Production endpoint selector survived rendering")
    args.output.write_text(result)


if __name__ == "__main__":
    main()
