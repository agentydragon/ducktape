"""Render a disposable, single-member KVM worker canary from an immutable image digest."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

NAME = "buildbuddy-kvm-worker-nixos-spike"
POOL = "kvm-vm-nixos-spike"
IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}$")


def render(image: str, executor_config: str) -> str:
    if not IMAGE_RE.fullmatch(image):
        raise ValueError("--image must be a full OCI reference pinned by sha256 digest")
    if "${BUILDBUDDY_API_KEY}" not in executor_config:
        raise ValueError("executor.yaml must get its API key from runtime substitution")
    if "kvm-vm" in executor_config:
        raise ValueError("executor.yaml must not set the production pool")

    resources = list(yaml.safe_load_all(Path(__file__).with_name("canary.yaml").read_text()))
    pool = next(resource for resource in resources if resource["kind"] == "VirtualMachinePool")
    pool["spec"]["virtualMachineTemplate"]["spec"]["template"]["spec"]["volumes"][0]["containerDisk"]["image"] = image
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": NAME, "namespace": "buildbuddy-executor"},
        "data": {
            "executor.yaml": executor_config,
            "k3s.yaml": "disable-default-registry-endpoint: true\n",
            "registries.yaml": yaml.safe_dump(
                {
                    "mirrors": {
                        registry: {
                            "endpoint": ["http://oci-cache.oci-cache.svc.cluster.local:80"],
                            "rewrite": {"^(.*)$": f"{prefix}/$1"},
                        }
                        for registry, prefix in (
                            ("docker.io", "docker-hub"),
                            ("ghcr.io", "ghcr"),
                            ("quay.io", "quay"),
                            ("registry.k8s.io", "k8s"),
                            ("gcr.io", "gcr"),
                        )
                    }
                },
                sort_keys=False,
            ),
            "pool": POOL + "\n",
        },
    }
    return yaml.safe_dump_all([config_map, *resources], sort_keys=False, explicit_start=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="ContainerDisk reference pinned by @sha256 digest")
    parser.add_argument("--executor-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(args.image, args.executor_config.read_text()))


if __name__ == "__main__":
    main()
