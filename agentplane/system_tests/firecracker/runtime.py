"""Firecracker's per-action KVM readiness and retained guest diagnostics."""

import json
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import yaml
from kubernetes.utils.quantity import parse_quantity
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.deployment.virtualization import RuntimeCapabilities
from agentplane.system_tests.k3s.baseline import K3s
from agentplane.system_tests.vm import guest_logs
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


class Runtime:
    def __init__(self) -> None:
        self.setup_objects: list[dict[str, Any]] = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "kvm-device-plugin"}},
            {
                "apiVersion": "apps/v1",
                "kind": "DaemonSet",
                "metadata": {"name": "kvm-device-plugin", "namespace": "kvm-device-plugin"},
                "spec": {
                    "selector": {"matchLabels": {"app": "kvm-device-plugin"}},
                    "template": {
                        "metadata": {"labels": {"app": "kvm-device-plugin"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "device-plugin",
                                    "image": os.environ["KVM_DEVICE_PLUGIN_IMAGE"],
                                    "args": [
                                        "--domain=devices.kubevirt.io",
                                        "--device=name: kvm\ngroups:\n  - paths:\n      - path: /dev/kvm\n    count: 1",
                                    ],
                                    "securityContext": {"privileged": True},
                                    "volumeMounts": [
                                        {"name": "plugins", "mountPath": "/var/lib/kubelet/device-plugins"},
                                        {"name": "kvm", "mountPath": "/dev/kvm"},
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "plugins",
                                    "hostPath": {"path": "/var/lib/kubelet/device-plugins", "type": "Directory"},
                                },
                                {"name": "kvm", "hostPath": {"path": "/dev/kvm", "type": "CharDevice"}},
                            ],
                        },
                    },
                },
            },
        ]

    def configure(self, objects: list[dict[str, Any]]) -> None:
        # The suite's image manifest selects the guest; no VM root/profile selection is needed.
        return None

    def install(self, k3s: K3s) -> None:
        k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(self.setup_objects))

    def wait_for_apis(self, k3s: K3s) -> None:
        # A device plugin uses only the core API and has no CRDs or admission webhook.
        return None

    def wait_until_ready(self, k3s: K3s) -> None:
        k3s.kubectl("rollout", "status", "daemonset/kvm-device-plugin", "-n", "kvm-device-plugin", "--timeout=60s")
        for attempt in Retrying(
            stop=stop_after_delay(60), wait=wait_fixed(1), retry=retry_if_exception_type(AssertionError), reraise=True
        ):
            with attempt:
                nodes = json.loads(k3s.kubectl("get", "nodes", "-o", "json"))["items"]
                assert any(
                    parse_quantity(node["status"].get("allocatable", {}).get("devices.kubevirt.io/kvm", "0")) > 0
                    for node in nodes
                ), "the KVM device plugin has not advertised its resource"

    def capture(self, k3s: K3s, namespace: str, validate_shutdown: Callable[[], bool]) -> AbstractContextManager[None]:
        return guest_logs.capture(
            log_root=Path("/var/log/pods"),
            pod_root=Path("/var/lib/kubelet/pods"),
            storage_root=k3s.data_dir / "storage",
            namespace=namespace,
            kubevirt_namespace="kvm-device-plugin",
            destination=undeclared_outputs_dir() / "vm-artifacts",
            validate_shutdown=validate_shutdown,
            require_shutdown_marker=False,
        )

    def report(self, k3s: K3s, namespace: str) -> str:
        return k3s.kubectl("get", "sandboxes,pods,pvc", "-n", namespace, "-o", "yaml", check=False)

    def clean(self, k3s: K3s) -> None:
        return None


def runtime(_work: Path) -> RuntimeCapabilities:
    return RuntimeCapabilities(guest=Runtime())
