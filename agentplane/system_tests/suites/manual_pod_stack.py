"""Start a fresh pod stack and retain its Kubernetes readiness evidence."""

import json
import subprocess
from pathlib import Path

from agentplane.system_tests.deployment.rendered_fixture import LocalModel
from agentplane.system_tests.deployment.stack import StackInputs, running_stack
from util.net import Listener


def run_manual_pod_stack(inputs: StackInputs, scripted_listener: Listener, output: Path) -> None:
    with running_stack(
        inputs,
        models=LocalModel(url=f"http://{scripted_listener.host}:{scripted_listener.port}"),
        runtime_factory=None,
        validate_vm_shutdown=lambda: True,
        persist_infrastructure=True,
    ) as stack:
        pods = stack.cluster.command("get", "pods", "-o", "json")
        # Keep Kubernetes' own conditions and transition times as the native readiness evidence.
        result = subprocess.run(pods, capture_output=True, text=True, check=True, timeout=30)
        evidence = json.loads(result.stdout)
        assert evidence["items"]
        assert all(
            any(
                condition["type"] == "Ready" and condition["status"] == "True"
                for condition in pod["status"]["conditions"]
            )
            for pod in evidence["items"]
        ), evidence
        (output / "ready-pods.json").write_text(result.stdout)
