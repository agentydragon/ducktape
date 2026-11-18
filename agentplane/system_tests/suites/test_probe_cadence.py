"""Manual comparison of real readiness on one worker, with fresh workloads per arm."""

import os
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from agentplane.system_tests.deployment.pytest_plugin import load_stack_inputs
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.suites.manual_pod_stack import run_manual_pod_stack
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import Listener

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)


@pytest.mark.parametrize("arm", ["warmup", "control-a", "fast-a", "fast-b", "control-b"])
def test_probe_cadence(arm: str, runfiles: Runfiles, k3s_binary: Path, scripted_listener: Listener) -> None:
    assert os.environ.get("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE") == "1"
    inputs = load_stack_inputs(runfiles, k3s_binary)
    original = inputs.fixture.model_copy(deep=True)
    for document in inputs.fixture.objects:
        if document["kind"] != "Deployment" or not arm.startswith("fast"):
            continue
        for container in document["spec"]["template"]["spec"]["containers"]:
            for name in ("startupProbe", "readinessProbe"):
                if name not in container:
                    continue
                probe = container[name]
                if name == "startupProbe":
                    # Preserve the startup failure budget while observing readiness more often.
                    probe["failureThreshold"] = probe.get("failureThreshold", 3) * probe.get("periodSeconds", 10)
                probe["periodSeconds"] = 1
                probe["initialDelaySeconds"] = 0
    output = undeclared_outputs_dir() / arm
    output.mkdir()
    with (
        pytest.MonkeyPatch.context() as environment,
        tracer.start_as_current_span("probe_cadence.arm", attributes={"experiment.arm": arm}),
    ):
        environment.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(output))
        (output / "original-fixture.json").write_text(original.model_dump_json())
        (output / "tested-fixture.json").write_text(inputs.fixture.model_dump_json())
        run_manual_pod_stack(inputs, scripted_listener, output)
