"""Run and resume pinned Codex with independently packaged workload tools."""

from __future__ import annotations

import json
import os
import shutil
import signal
import tarfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.env.e2e.testing import events, launches
from agentplane.env.e2e.testing.driver import RunnerDriver
from agentplane.env.e2e.testing.process_fixtures import RunnerProcess
from agentplane.proto import event_pb2, session_pb2
from agentplane.testing.scripted_model import ScriptedModel, ShellCall, Text
from util.bazel.runfiles import get_required_path, own_repo_rlocation

# gazelle:include_dep @pypi//protobuf


def _extract_oci_rootfs(rloc_file: str, destination: Path) -> None:
    """Expand the layers from a Bazel OCI layout into a disposable child root."""
    reference = get_required_path(own_repo_rlocation(rloc_file))
    layout = get_required_path(reference.read_text().strip())
    index = json.loads((layout / "index.json").read_text())
    (descriptor,) = index["manifests"]
    manifest_path = layout / "blobs" / descriptor["digest"].replace(":", "/")
    manifest = json.loads(manifest_path.read_text())
    for layer in manifest["layers"]:
        layer_path = layout / "blobs" / layer["digest"].replace(":", "/")
        with tarfile.open(layer_path, "r:*") as archive:
            archive.extractall(destination, filter="data")


@pytest.mark.parametrize("harness", [session_pb2.HARNESS_CODEX], ids=["codex"])
async def test_sparse_child_root_completes_turn_and_resumes_after_harness_exit(
    harness: session_pb2.Harness,
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    driver: RunnerDriver,
    tmp_path: Path,
) -> None:
    """Pinned Codex runs a workload-image tool and keeps workspace/session state across restart."""
    child_spec = session_pb2.SessionSpec()
    child_spec.CopyFrom(spec)
    child_spec.cwd = "/workspace"
    child_root = tmp_path / "child-root"
    codex = get_required_path(launches.CODEX_BINARY)
    binary_dir = child_root / "opt/bin"
    binary_dir.mkdir(parents=True)
    shutil.copyfile(codex, binary_dir / "codex")
    (binary_dir / "codex").chmod(0o555)
    codex_bwrap = get_required_path("agentplane_codex_cli_linux_x64/codex-resources/bwrap")
    resources_dir = child_root / "opt/codex-resources"
    resources_dir.mkdir()
    shutil.copyfile(codex_bwrap, resources_dir / "bwrap")
    (resources_dir / "bwrap").chmod(0o555)
    _extract_oci_rootfs("agentplane/x/runner_layering/tools_rootfs.rloc", child_root)
    assert (child_root / "usr/local/bin/workload-tool").is_file()

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    private_sentinel = state_dir / "runner-private-sentinel"
    private_sentinel.write_text("runner-owned\n")
    workload_proof = Path(spec.cwd) / "workload-image-proof.txt"
    assert not workload_proof.exists()
    child_exec = get_required_path(own_repo_rlocation("agentplane/x/runner_layering/child_exec_/child_exec"))
    first_runner = await start_runner(
        codex_binary=child_exec,
        harness_env=[
            f"AGENTPLANE_CHILD_ROOT={child_root}",
            f"AGENTPLANE_CHILD_WORKSPACE={spec.cwd}",
            f"AGENTPLANE_PRIVATE_SENTINEL={private_sentinel}",
        ],
    )

    session = await driver.attach("sparse-child-root", spec=child_spec)
    await session.submit_input("input-1", "Reply with exactly: SPARSE_ROOT_TURN_OK")
    request = await model.request()
    await model.reply(request, Text("SPARSE_ROOT_TURN_OK"))
    await session.until(events.turn_completed)
    fences = [
        entry
        for entry in events.of_kind(session.seen, "harness_stderr")
        if "runner-private state remains outside child root" in entry.event.harness_stderr.text
    ]
    assert len(fences) == 1
    fence = fences[0]
    assert fence.event.harness_stderr.text.strip() == "runner-layering: runner-private state remains outside child root"

    await session.submit_input("input-2", "Run the tool installed by the workload image and report its output.")
    request = await model.request()
    await model.reply(request, ShellCall("workload-image-tool", "/usr/local/bin/workload-tool"))
    tool_result = await model.request()
    (output,) = tool_result.tool_outputs
    stderr = "\n".join(entry.event.harness_stderr.text for entry in events.of_kind(session.seen, "harness_stderr"))
    assert output.contains("WORKLOAD_IMAGE_TOOL_OK"), f"tool output: {output.blocks}\nharness stderr:\n{stderr}"
    await model.reply(tool_result, Text("WORKLOAD_IMAGE_TOOL_DONE"))
    tool_done = await session.until(events.turn_completed)
    assert tool_done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED

    (started,) = events.of_kind(session.seen, "harness_started")
    os.kill(started.event.harness_started.pid, signal.SIGKILL)
    exited = await session.until(events.is_kind("harness_exited"))
    assert exited.cursor > fence.cursor
    assert not exited.event.harness_exited.stopped_by_runner
    assert first_runner.process.returncode is None, "a native child exit must not terminate the runner"
    await session.drain_until_end()

    second = await driver.attach("sparse-child-root", spec=child_spec, after_cursor=exited.cursor)
    await second.submit_input("input-3", "Reply with exactly: SPARSE_ROOT_RESUME_OK")
    resumed = await second.until(
        lambda entry: events.is_kind("harness_started")(entry) and entry.event.harness_started.resumed
    )
    assert resumed.cursor > exited.cursor
    assert workload_proof.read_text() == "WORKLOAD_IMAGE_TOOL_OK\n"
    request = await model.request()
    assert request.user_texts == [
        "Reply with exactly: SPARSE_ROOT_TURN_OK",
        "Run the tool installed by the workload image and report its output.",
        "Reply with exactly: SPARSE_ROOT_RESUME_OK",
    ]
    assert request.assistant_texts == ["SPARSE_ROOT_TURN_OK", "WORKLOAD_IMAGE_TOOL_DONE"]
    await model.reply(request, Text("SPARSE_ROOT_RESUME_OK"))
    done = await second.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    assert private_sentinel.read_text() == "runner-owned\n"

    await second.submit_input("input-4", "Run the workload image tool again and report its output.")
    request = await model.request()
    assert request.user_texts == [
        "Reply with exactly: SPARSE_ROOT_TURN_OK",
        "Run the tool installed by the workload image and report its output.",
        "Reply with exactly: SPARSE_ROOT_RESUME_OK",
        "Run the workload image tool again and report its output.",
    ]
    assert request.assistant_texts == ["SPARSE_ROOT_TURN_OK", "WORKLOAD_IMAGE_TOOL_DONE", "SPARSE_ROOT_RESUME_OK"]
    resumed_call_id = "workload-image-tool-after-resume"
    await model.reply(request, ShellCall(resumed_call_id, "/usr/local/bin/workload-tool"))
    resumed_tool_result = await model.request()
    (resumed_output,) = [output for output in resumed_tool_result.tool_outputs if output.call_id == resumed_call_id]
    resumed_stderr = "\n".join(
        entry.event.harness_stderr.text for entry in events.of_kind(second.seen, "harness_stderr")
    )
    assert resumed_output.contains("WORKLOAD_IMAGE_TOOL_OK"), (
        f"resumed tool output: {resumed_output.blocks}\nharness stderr:\n{resumed_stderr}"
    )
    assert workload_proof.read_text() == "WORKLOAD_IMAGE_TOOL_OK\n"
    await model.reply(resumed_tool_result, Text("WORKLOAD_IMAGE_TOOL_AFTER_RESUME_DONE"))
    resumed_tool_done = await second.until(events.turn_completed)
    assert resumed_tool_done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED


if __name__ == "__main__":
    pytest_bazel.main()
