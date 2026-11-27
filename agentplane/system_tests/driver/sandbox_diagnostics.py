"""Sandbox runner data capture and cleanup diagnostics for deployed system tests."""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from python.runfiles import Runfiles

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.host_command import run_host_command
from agentplane.system_tests.driver.sandbox import ENVIRONMENT_RUNTIME_CONTAINER
from agentplane.system_tests.driver.tracing import tracer
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

CLEANUP_DIAGNOSTICS_SECONDS = 30
RUNNER_DATA_CAPTURE_SECONDS = 120
RUNNER_DATA_MOUNTS = ("/state", "/state/workspaces", "/home/runner", "/tmp")


async def container_mount_sources(
    crictl_command: tuple[str, ...], cluster: Cluster, name: str, artifact_dir: Path
) -> dict[str, Path]:
    pod = json.loads(await cluster.kubectl("get", "pod", name, "-o", "json"))
    statuses = pod.get("status", {}).get("containerStatuses", [])
    environment_runtime = next(
        (status for status in statuses if status.get("name") == ENVIRONMENT_RUNTIME_CONTAINER), None
    )
    if environment_runtime is None or not environment_runtime.get("containerID"):
        raise RuntimeError("Environment Runtime container id is unavailable")
    container_id = environment_runtime["containerID"].removeprefix("containerd://")
    crictl_config = artifact_dir / "crictl.yaml"
    crictl_config.write_text("runtime-endpoint: unix:///run/k3s/containerd/containerd.sock\ntimeout: 30\n")
    inspected = json.loads(
        await run_host_command(
            (*crictl_command, "--config", str(crictl_config), "inspect", container_id),
            timeout_s=CLEANUP_DIAGNOSTICS_SECONDS,
        )
    )
    runtime_spec = inspected.get("info", {}).get("runtimeSpec", {})
    if isinstance(runtime_spec, str):
        runtime_spec = json.loads(runtime_spec)
    mounts = runtime_spec.get("mounts", [])
    sources: dict[str, Path] = {}
    for destination in RUNNER_DATA_MOUNTS:
        mount = next((item for item in mounts if item.get("destination") == destination), None)
        if mount is None or not mount.get("source"):
            raise RuntimeError(f"container runtime did not expose source for {destination}")
        source = Path(mount["source"])
        if not await asyncio.to_thread(source.exists):
            raise FileNotFoundError(f"container mount source for {destination} is not visible: {source}")
        sources[destination] = source
    return sources


async def archive_runner_data(
    crictl_command: tuple[str, ...],
    cluster: Cluster,
    name: str,
    session_ids: list[str],
    artifact_dir: Path,
    *,
    mount_sources: dict[str, Path] | None = None,
    extra_backups: tuple[tuple[str, Path], ...] = (),
) -> dict[str, Any]:
    sources = mount_sources or await container_mount_sources(crictl_command, cluster, name, artifact_dir)
    archive = artifact_dir / "runner-data.tar"
    partial_archive = artifact_dir / "runner-data.tar.partial"
    journal_dir = artifact_dir / "consistent-journals"
    state_source = sources["/state"]
    backups = [
        {
            "name": session_id,
            "source": str(state_source / "sessions" / session_id / "journal.sqlite"),
            "destination": str(journal_dir / f"session-{session_id}.sqlite"),
        }
        for session_id in session_ids
    ]
    backups.extend(
        {"name": name, "source": str(source), "destination": str(journal_dir / f"{name}.sqlite")}
        for name, source in extra_backups
    )
    plan_path = artifact_dir / "archive-plan.json"
    manifest_path = artifact_dir / "archive-manifest.json"
    plan_path.write_text(
        json.dumps(
            {
                "archive": str(archive),
                "partial_archive": str(partial_archive),
                "manifest": str(manifest_path),
                "mounts": [
                    {"destination": destination, "source": str(sources[destination])}
                    for destination in RUNNER_DATA_MOUNTS
                ],
                "backups": backups,
            },
            indent=2,
        )
        + "\n"
    )
    # A separate process lets timeout/cancellation stop SQLite backup and filesystem traversal
    # before cleanup removes their sources; cancelling a worker thread would leave them running.
    process = await asyncio.create_subprocess_exec(
        Runfiles.CreateOrRaise().root() / "ducktape/agentplane/system_tests/driver/runner_data_archive_bin",
        plan_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(RUNNER_DATA_CAPTURE_SECONDS):
            stdout, stderr = await process.communicate()
    except BaseException as error:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(error, TimeoutError):
            (artifact_dir / "archive-timeout.txt").write_text(
                f"Runner data capture exceeded {RUNNER_DATA_CAPTURE_SECONDS}s; partial files were retained.\n"
            )
        raise
    if not manifest_path.exists():
        raise RuntimeError(
            f"runner data archiver exited {process.returncode} without a manifest: "
            f"{stderr.decode(errors='replace')[-2000:]} {stdout.decode(errors='replace')[-2000:]}"
        )
    manifest: dict[str, Any] = json.loads(manifest_path.read_text())
    manifest["mount_sources"] = {destination: str(source) for destination, source in sources.items()}
    if process.returncode:
        manifest["archiver_exit_code"] = process.returncode
    return manifest


async def capture_cleanup_diagnostics(
    crictl_command: tuple[str, ...], cluster: Cluster, name: str, session_ids: list[str], command_ids: list[str]
) -> None:
    with tracer.start_as_current_span("system_test.dispatcher.diagnostics"):
        capture_id = uuid4().hex
        artifact_dir = undeclared_outputs_dir() / f"dispatcher-cleanup-{name}-{capture_id}"
        artifact_dir.mkdir(parents=True)
        diagnostics: dict[str, object] = {
            "sandbox": name,
            "capture_id": capture_id,
            "session_ids": session_ids,
            "command_ids": command_ids,
        }
        diagnostic_path = artifact_dir / "diagnostics.json"

        def persist() -> None:
            diagnostic_path.write_text(json.dumps(diagnostics, indent=2) + "\n")

        persist()
        commands = {
            "sandbox": ("get", "sandbox", name, "-o", "json"),
            "pod": ("get", "pod", name, "-o", "json"),
            "environment_runtime_log": (
                "logs",
                f"pod/{name}",
                "-c",
                ENVIRONMENT_RUNTIME_CONTAINER,
                "--since=10m",
                "--timestamps",
            ),
            "previous_environment_runtime_log": (
                "logs",
                f"pod/{name}",
                "-c",
                ENVIRONMENT_RUNTIME_CONTAINER,
                "--previous",
                "--since=10m",
                "--timestamps",
            ),
            "executor_log": ("logs", f"pod/{name}", "-c", "codex-executor", "--since=10m", "--timestamps"),
            "previous_executor_log": (
                "logs",
                f"pod/{name}",
                "-c",
                "codex-executor",
                "--previous",
                "--since=10m",
                "--timestamps",
            ),
            "sandbox_controller_log": (
                "logs",
                "deployment/environments",
                "--all-containers",
                "--since=10m",
                "--timestamps",
            ),
        }

        async def capture(key: str, args: tuple[str, ...]) -> None:
            try:
                async with asyncio.timeout(CLEANUP_DIAGNOSTICS_SECONDS):
                    value = await cluster.kubectl(*args)
                try:
                    diagnostics[key] = json.loads(value)
                except json.JSONDecodeError:
                    diagnostics[key] = value
            except TimeoutError:
                diagnostics[key] = f"TimeoutError: exceeded {CLEANUP_DIAGNOSTICS_SECONDS}s diagnostic deadline"
            except Exception as error:
                diagnostics[key] = {"capture_error": type(error).__name__}

        await asyncio.gather(*(capture(key, args) for key, args in commands.items()))
        # A Pod image volume can fail before its containers start. Capture both CRI's view and
        # containerd's image references while the failed Pod still exists, before cleanup.
        crictl_config = artifact_dir / "crictl.yaml"
        crictl_config.write_text("runtime-endpoint: unix:///run/k3s/containerd/containerd.sock\ntimeout: 10\n")
        image_commands = {
            "cri_images": (*crictl_command, "--config", str(crictl_config), "images", "-o", "json"),
            "containerd_images": (
                crictl_command[0],
                "ctr",
                "--address",
                "/run/k3s/containerd/containerd.sock",
                "-n",
                "k8s.io",
                "images",
                "list",
                "--quiet",
            ),
        }
        pod = diagnostics.get("pod")
        if isinstance(pod, dict):
            for volume in pod.get("spec", {}).get("volumes", []):
                if reference := volume.get("image", {}).get("reference"):
                    image_commands[f"cri_image_volume_{volume['name']}"] = (
                        *crictl_command,
                        "--config",
                        str(crictl_config),
                        "inspecti",
                        reference,
                    )

        async def capture_image_command(key: str, command: tuple[str, ...]) -> None:
            try:
                result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=10)
                diagnostics[key] = {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            except (OSError, subprocess.TimeoutExpired) as error:
                diagnostics[key] = {"capture_error": str(error)}

        await asyncio.gather(*(capture_image_command(key, command) for key, command in image_commands.items()))
        persist()
        try:
            diagnostics["runner_data"] = await archive_runner_data(
                crictl_command, cluster, name, session_ids, artifact_dir
            )
        except Exception as error:
            diagnostics["runner_data_error"] = f"{type(error).__name__}: {error}"
        persist()
