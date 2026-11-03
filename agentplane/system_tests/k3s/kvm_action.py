"""Prepare the private OCI action namespaces for the nested k3s node."""

import asyncio
import fcntl
import json
import subprocess
from pathlib import Path
from typing import Any

from agentplane.environments.v1 import environment_service_pb2
from agentplane.system_tests.k3s import vm_sandboxes
from agentplane.system_tests.sandbox import Sandboxes
from util.testing.undeclared_outputs import undeclared_outputs_dir

# Generated EnvironmentDescriptor stubs import protobuf's well-known types.
# gazelle:include_dep @pypi//protobuf


def prepare() -> None:
    # Fail before starting k3s if the selected worker cannot provide KVM.
    with Path("/dev/kvm").open("r+b", buffering=0) as device:
        version = fcntl.ioctl(device, 0xAE00)
    if version != 12:
        raise RuntimeError(f"unsupported KVM API version: {version}")
    # OCI's default mounts are read-only. Fresh mounts use this action's network
    # and cgroup namespaces, including its own delegated cgroup subtree.
    subprocess.run(["mount", "-t", "sysfs", "sysfs", "/sys"], check=True)
    subprocess.run(["mount", "-t", "cgroup2", "cgroup2", "/sys/fs/cgroup"], check=True)
    subprocess.run(["mount", "-o", "remount,rw", "/proc/sys"], check=True)


def verify_acceleration(
    sandboxes: Sandboxes, kubectl: vm_sandboxes.Kubectl, namespace: str, accelerator: vm_sandboxes.Accelerator
) -> Sandboxes:
    if accelerator is vm_sandboxes.Accelerator.TCG:
        return sandboxes

    async def create(name: str, **kwargs: Any) -> environment_service_pb2.EnvironmentDescriptor:
        created = await sandboxes(name, **kwargs)
        evidence = await asyncio.to_thread(vm_sandboxes.assert_kvm, kubectl, namespace, vmi_name=created.name)
        directory = undeclared_outputs_dir() / "vm-artifacts/kvm"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{created.name}.json").write_text(json.dumps(evidence, indent=2) + "\n")
        return created

    return create
