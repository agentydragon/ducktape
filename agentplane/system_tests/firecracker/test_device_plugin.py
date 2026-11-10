"""Hosted allocation smoke: use a null character device where hosted RBE has no KVM."""

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from python.runfiles import Runfiles

from agentplane.system_tests.firecracker.runtime import Runtime
from agentplane.system_tests.k3s.baseline import K3s


def test_device_allocation_releases_after_pod_deletion(tmp_path: Path, runfiles: Runfiles, k3s_binary: Path) -> None:
    node = K3s(tmp_path, k3s_binary)
    images = json.loads((runfiles.root() / os.environ["KVM_PLUGIN_MANIFEST_RLOCATION"]).read_text())["fixture"]
    with contextlib.ExitStack() as cleanup:
        process = node.start(layers_in_memory=True)
        cleanup.callback(node.stop, process)
        node.import_images({image: runfiles.root() / location for image, location in images.items()})
        runtime = Runtime(tmp_path)
        # This tests Kubernetes device registration/allocation/release, not KVM ioctls.
        # The production Firecracker acceptance retains /dev/kvm and its unchanged guest.
        container = runtime.releases[1]["spec"]["template"]["spec"]["containers"][0]
        container["args"][1] = (
            "--device=name: kvm\ngroups:\n  - paths:\n      - path: /dev/null\n        mountPath: /dev/kvm\n    count: 1"
        )
        runtime.releases[1]["spec"]["template"]["spec"]["volumes"][1]["hostPath"] = {
            "path": "/dev/null",
            "type": "CharDevice",
        }
        runtime.releases[1]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][1]["mountPath"] = "/dev/null"
        runtime.install(node)
        runtime.wait_until_ready(node)
        consumer_image = os.environ["KVM_PLUGIN_CONSUMER_IMAGE"]

        def consumer(name: str) -> dict[str, Any]:
            return {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "consumer",
                            "image": consumer_image,
                            "command": ["sh", "-c", "test -c /dev/kvm && echo allocated && exec sleep 600"],
                            "resources": {"limits": {"devices.kubevirt.io/kvm": "1"}},
                        }
                    ],
                },
            }

        node.kubectl("apply", "-f", "-", input_text=yaml.safe_dump(consumer("first")))
        node.kubectl("wait", "--for=condition=Ready", "pod/first", "--timeout=60s")
        assert "allocated" in node.kubectl("logs", "first")
        node.kubectl("apply", "-f", "-", input_text=yaml.safe_dump(consumer("second")))
        node.kubectl(
            "wait",
            '--for=jsonpath={.status.conditions[?(@.type=="PodScheduled")].reason}=Unschedulable',
            "pod/second",
            "--timeout=30s",
        )
        second = json.loads(node.kubectl("get", "pod/second", "-o", "json"))
        assert any(
            "Insufficient devices.kubevirt.io/kvm" in condition.get("message", "")
            for condition in second["status"]["conditions"]
        )
        node.kubectl("delete", "pod/first", "--grace-period=0", "--force", "--wait=true")
        node.kubectl("wait", "--for=condition=Ready", "pod/second", "--timeout=60s")
        assert "allocated" in node.kubectl("logs", "second")


if __name__ == "__main__":
    pytest_bazel.main()
