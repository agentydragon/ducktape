"""Manual import-transaction comparison on one worker, with fresh workloads per arm."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from agentplane.system_tests.deployment.rendered_fixture import LocalModel
from agentplane.system_tests.deployment.stack import Stack, running_stack
from agentplane.system_tests.deployment.system_test_target import load_stack_inputs
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.k3s.baseline import K3s
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import Listener


@pytest.fixture(scope="session")
def deployment() -> None:
    """This experiment owns each fresh stack instead of the ordinary session fixture."""


@pytest.mark.parametrize("arm", ["warmup", "control-a", "fast-a", "fast-b", "control-b"])
def test_import_transactions(arm: str, runfiles: Runfiles, k3s_binary: Path, scripted_listener: Listener) -> None:
    assert os.environ.get("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE") == "1"
    inputs = load_stack_inputs(runfiles, k3s_binary)
    original = inputs.fixture.model_copy(deep=True)
    output = undeclared_outputs_dir() / arm
    output.mkdir()
    with (
        pytest.MonkeyPatch.context() as environment,
        tracer.start_as_current_span("import_transactions.arm", attributes={"experiment.arm": arm}),
    ):
        if not arm.startswith("fast"):
            environment.setattr(K3s, "_usable_images", lambda _node, _images: set())
        environment.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(output))
        (output / "original-fixture.json").write_text(original.model_dump_json())
        (output / "tested-fixture.json").write_text(inputs.fixture.model_dump_json())
        with running_stack(
            inputs,
            models=LocalModel(url=f"http://{scripted_listener.host}:{scripted_listener.port}"),
            virtualization_factory=None,
            validate_vm_shutdown=lambda: True,
            persist_infrastructure=True,
        ) as stack:
            _assert_ready(stack, output)


def _assert_ready(stack: Stack, output: Path) -> None:
    pods = stack.cluster.command("get", "pods", "-o", "json")
    # Keep Kubernetes' own conditions and transition times as the native readiness evidence.
    result = subprocess.run(pods, capture_output=True, text=True, check=True, timeout=30)
    evidence = json.loads(result.stdout)
    assert evidence["items"]
    assert all(
        any(condition["type"] == "Ready" and condition["status"] == "True" for condition in pod["status"]["conditions"])
        for pod in evidence["items"]
    ), evidence
    (output / "ready-pods.json").write_text(result.stdout)


if __name__ == "__main__":
    pytest_bazel.main()
