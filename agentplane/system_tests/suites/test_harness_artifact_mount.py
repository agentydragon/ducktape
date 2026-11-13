"""Manual real-harness turns with OCI packaging or read-only declared artifacts."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from agentplane.proto import session_pb2
from agentplane.system_tests.deployment.rendered_fixture import LocalModel
from agentplane.system_tests.deployment.stack import StackInputs, running_stack
from agentplane.system_tests.deployment.system_test_target import load_stack_inputs
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.scenario import open_resources
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.scenarios import session_instructions
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import Listener


@pytest.fixture(scope="session")
def deployment() -> None:
    """The experiment owns its ordinary stack lifecycle."""


def _mount_artifacts(inputs: StackInputs, runfiles: Runfiles) -> None:
    claude = runfiles.root() / os.environ["HARNESS_CLAUDE_RLOCATION"]
    codex = runfiles.root() / os.environ["HARNESS_CODEX_RLOCATION"]
    sources = [(claude.resolve(), "/opt/harnesses/claude", "File")]
    codex_root = codex.parent.parent
    # Each declared file is mounted directly. Resolving a whole runfiles directory
    # leaves its absolute file-cache symlinks dangling inside the Pod's root.
    sources.extend(
        (path.resolve(), "/opt/harnesses/codex/" + path.relative_to(codex_root).as_posix(), "File")
        for path in sorted(codex_root.rglob("*"))
        if path.is_file()
    )
    evidence = []
    for document in inputs.fixture.objects:
        if document["kind"] != "SandboxTemplate" or "pod" not in document["spec"]:
            continue
        spec = document["spec"]["pod"]["spec"]
        workloads = [container for container in spec["containers"] if "/environment-tools:" in container["image"]]
        if not workloads:
            continue
        for index, (source, destination, kind) in enumerate(sources):
            name = f"harness-artifact-{index}"
            spec["volumes"].append({"name": name, "hostPath": {"path": str(source), "type": kind}})
            for container in workloads:
                container["volumeMounts"].append({"name": name, "mountPath": destination, "readOnly": True})
            evidence.append({"source": str(source), "destination": destination, "mode": source.stat().st_mode})
    assert evidence
    (undeclared_outputs_dir() / "artifact-mounts.json").write_text(json.dumps(evidence, indent=2))


async def test_harness_turns(runfiles: Runfiles, k3s_binary: Path, scripted_listener: Listener, tmp_path: Path) -> None:
    inputs = load_stack_inputs(runfiles, k3s_binary)
    mounted = os.environ["HARNESS_ARTIFACT_MOUNTS"] == "1"
    if mounted:
        _mount_artifacts(inputs, runfiles)
    (undeclared_outputs_dir() / "tested-fixture.json").write_text(inputs.fixture.model_dump_json())
    with running_stack(
        inputs,
        models=LocalModel(url=f"http://{scripted_listener.host}:{scripted_listener.port}"),
        virtualization_factory=None,
        validate_vm_shutdown=lambda: True,
        persist_infrastructure=True,
    ) as stack:
        upstream = ScriptedUpstream()
        async with (
            upstream.serving(listener=scripted_listener.socket),
            native_client(stack.cluster, tmp_path / "dispatcher-token") as dispatcher,
            open_resources(stack.cluster, stack.base_url, stack.token, tracer) as (client, create),
        ):
            models = await models_offered_for(client, [session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX])
            for harness in (session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX):
                view = await create("artifact-mount-" + uuid4().hex[:8])
                pod = view.kubernetes_diagnostics.pod
                proof = await stack.cluster.kubectl(
                    "exec",
                    pod.name,
                    "-c",
                    "environment-runtime",
                    "--",
                    "sh",
                    "-ec",
                    "test ! -e /home/runner/artifact-probe; touch /home/runner/artifact-probe; "
                    "test -x /opt/harnesses/claude; test -x /opt/harnesses/codex/bin/codex; "
                    "claude --version; codex --version; cat /proc/self/mountinfo",
                )
                if mounted:
                    mounts = [line.split() for line in proof.splitlines() if " /opt/harnesses/" in line]
                    assert len(mounts) >= 2
                    assert all("ro" in fields[5].split(",") for fields in mounts)
                (undeclared_outputs_dir() / f"harness-{harness}-mounts.txt").write_text(proof)
                await session_instructions.the_standing_instruction_reaches_the_model_and_only_in_its_own_session(
                    client, dispatcher, view, harness, models[harness], upstream
                )


if __name__ == "__main__":
    pytest_bazel.main()
