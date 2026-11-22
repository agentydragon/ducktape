"""Render a disposable, single-member KVM worker canary from an immutable image digest."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml
from python.runfiles import Runfiles

NAME = "buildbuddy-kvm-worker-nixos-spike"
POOL = "kvm-vm-nixos-spike"
IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}$")


def render(image: str, executor_config: str | None = None) -> str:
    if not IMAGE_RE.fullmatch(image):
        raise ValueError("--image must be a full OCI reference pinned by sha256 digest")
    worker = (
        Runfiles.CreateOrRaise().root() / "ducktape/cluster/k8s/buildbuddy-kvm-worker/buildbuddy-kvm-worker.k8s.yaml"
    )
    [guest_config] = [
        doc["data"]
        for doc in yaml.safe_load_all(worker.read_text())
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "buildbuddy-kvm-worker"
    ]
    if executor_config is None:
        executor_config = guest_config["executor.yaml"]
    if "${BUILDBUDDY_API_KEY}" not in executor_config:
        raise ValueError("executor.yaml must get its API key from runtime substitution")
    if "kvm-vm" in executor_config:
        raise ValueError("executor.yaml must not set the production pool")

    resources = list(yaml.safe_load_all(Path(__file__).with_name("canary.yaml").read_text()))
    pool = next(resource for resource in resources if resource["kind"] == "VirtualMachinePool")
    volumes = pool["spec"]["virtualMachineTemplate"]["spec"]["template"]["spec"]["volumes"]
    [disk] = [v["containerDisk"] for v in volumes if "containerDisk" in v]
    disk["image"] = image
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": NAME, "namespace": "buildbuddy-executor"},
        "data": {
            "executor.yaml": executor_config,
            "k3s.yaml": guest_config["k3s.yaml"],
            "registries.yaml": guest_config["registries.yaml"],
            "pool": POOL + "\n",
        },
    }
    return yaml.safe_dump_all([config_map, *resources], sort_keys=False, explicit_start=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="ContainerDisk reference pinned by @sha256 digest")
    parser.add_argument("--executor-config", type=Path, help="Override the production worker's executor.yaml")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(args.image, args.executor_config.read_text() if args.executor_config else None))


if __name__ == "__main__":
    main()
