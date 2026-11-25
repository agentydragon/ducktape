"""One real VM failure that qualifies retained-baseline and raw-volume teardown ordering."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import inspect
import json
import os
import stat
import struct
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.sandbox_resource import SANDBOX_API_VERSION
from agentplane.system_tests.deployment import rendered_fixture, stack as stack_module
from agentplane.system_tests.deployment.stack import StackInputs, running_stack
from agentplane.system_tests.driver.identity import API_VERSION as IDENTITY_API_VERSION, LAUNCH_DEFAULT_POLICY_SETS
from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.vm.configuration import SUPERVISION_TEMPLATE
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import Listener

pytest_plugins = ("agentplane.system_tests.vm.pytest_plugin",)
pytestmark = [pytest.mark.vm_sandboxes, pytest.mark.scripted_model]

_TASK_ROOT = Path("/run/k3s/containerd/io.containerd.runtime.v2.task/k8s.io")
_LOOP_SYSFS = Path("/sys/block")
_LOOP_GET_STATUS64 = 0x4C05
_LOOP_INFO64_SIZE = 232
_LOOP_FLAGS_OFFSET = 52


class _InjectedFailureError(RuntimeError):
    pass


def _save(path: Path, evidence: dict[str, Any]) -> None:
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def _loop_backing(index: int) -> str:
    try:
        return (_LOOP_SYSFS / f"loop{index}/loop/backing_file").read_text().strip()
    except FileNotFoundError:
        return ""


def _encode_loop_device_number(device: int) -> int:
    major = os.major(device)
    minor = os.minor(device)
    return (minor & 0xFF) | (major << 8) | ((minor & ~0xFF) << 12)


def _dependent_loop_mappings(owned_device: int) -> list[dict[str, Any]]:
    """Read kernel loop identities directly for the qualification receipt."""
    expected_rdevice = _encode_loop_device_number(owned_device)
    mappings: list[dict[str, Any]] = []
    for path in sorted(Path("/dev").glob("loop[0-9]*")):
        suffix = path.name.removeprefix("loop")
        if not suffix.isdecimal():
            continue
        index = int(suffix)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ENXIO):
                continue
            raise
        try:
            device_stat = os.fstat(fd)
            if (
                not stat.S_ISBLK(device_stat.st_mode)
                or os.major(device_stat.st_rdev) != 7
                or os.minor(device_stat.st_rdev) != index
            ):
                raise RuntimeError(f"loop path changed identity while observing {path}")
            info = bytearray(_LOOP_INFO64_SIZE)
            try:
                fcntl.ioctl(fd, _LOOP_GET_STATUS64, info, True)
            except OSError as error:
                if error.errno == errno.ENXIO:
                    continue
                raise
            lo_device, lo_inode, lo_rdevice = struct.unpack_from("=QQQ", info)
            if lo_rdevice == expected_rdevice:
                lo_flags = struct.unpack_from("=I", info, _LOOP_FLAGS_OFFSET)[0]
                mappings.append(
                    {
                        "path": str(path),
                        "index": index,
                        "block_device": device_stat.st_rdev,
                        "device": lo_device,
                        "inode": lo_inode,
                        "rdevice": lo_rdevice,
                        "flags": lo_flags,
                        "backing_file": _loop_backing(index),
                    }
                )
        finally:
            os.close(fd)
    return mappings


def _container_tasks() -> dict[str, dict[str, int | str | None]]:
    # Containerd task state uses K3s's runtime root, distinct from the persistent data root.
    root = _TASK_ROOT
    tasks: dict[str, dict[str, int | str | None]] = {}
    if not root.exists():
        return tasks
    for pid_file in sorted(root.glob("*/init.pid")):
        try:
            pid = int(pid_file.read_text().strip())
        except FileNotFoundError, ValueError:
            continue
        tasks[pid_file.parent.name] = {"pid": pid, "start_ticks": baseline._baseline_process_start(pid)}
    return tasks


def _block_device_users(index: int) -> dict[str, dict[str, Any]]:
    """Find processes holding an open FD for this exact host loop block device."""
    expected_device = os.makedev(7, index)
    users: dict[str, dict[str, Any]] = {}
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        pid = int(process.name)
        # BlockStorage deliberately holds this action's own loop FD until after stop_owned.
        if pid == os.getpid():
            continue
        start_ticks = baseline._baseline_process_start(pid)
        if start_ticks is None:
            continue
        matches: list[dict[str, str]] = []
        try:
            descriptors = list((process / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                info = descriptor.stat()
                if stat.S_ISBLK(info.st_mode) and info.st_rdev == expected_device:
                    matches.append({"fd": descriptor.name, "target": str(descriptor.readlink())})
            except OSError:
                continue
        if matches:
            try:
                command = (process / "comm").read_text().strip()
            except OSError:
                command = ""
            try:
                cgroup = (process / "cgroup").read_text().strip()
            except OSError:
                cgroup = ""
            ancestors: list[int] = []
            ancestor = pid
            for _ in range(32):
                if ancestor <= 1 or ancestor in ancestors:
                    break
                ancestors.append(ancestor)
                try:
                    status = (Path("/proc") / str(ancestor) / "status").read_text()
                    parent_line = next(line for line in status.splitlines() if line.startswith("PPid:"))
                    ancestor = int(parent_line.split()[1])
                except OSError, StopIteration, ValueError:
                    break
            users[str(pid)] = {
                "pid": pid,
                "start_ticks": start_ticks,
                "comm": command,
                "cgroup": cgroup,
                "ancestor_pids": ancestors,
                "fds": matches,
            }
    return users


def _resource(k3s: baseline.K3s, *args: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(k3s.kubectl(*args, "-o", "json")))


def _owner_uid(resource: dict[str, Any], uid: str) -> bool:
    return any(owner.get("uid") == uid for owner in resource.get("metadata", {}).get("ownerReferences", []))


def _launcher_for_vmi(k3s: baseline.K3s, namespace: str, vmi: dict[str, Any]) -> dict[str, Any]:
    pods = _resource(k3s, "get", "pods", "-n", namespace)["items"]
    launchers = [
        pod
        for pod in pods
        if pod.get("metadata", {}).get("labels", {}).get("kubevirt.io") == "virt-launcher"
        and _owner_uid(pod, vmi["metadata"]["uid"])
    ]
    if len(launchers) != 1:
        raise AssertionError(f"expected one virt-launcher for VMI {vmi['metadata']['name']}, got {len(launchers)}")
    return cast(dict[str, Any], launchers[0])


def _task_pid(task: dict[str, int | str | None]) -> int:
    pid = task["pid"]
    if not isinstance(pid, int):
        raise AssertionError(f"container task has invalid init PID: {pid!r}")
    return pid


def _run_snapshot(
    k3s: baseline.K3s,
    marker: dict[str, Any],
    namespace: str,
    sandbox_name: str,
    sandbox_uid: str,
    loop_index: int,
    backing_file: str,
    device_fd: int,
) -> dict[str, Any]:
    sandbox = _resource(k3s, "get", "sandbox", sandbox_name, "-n", namespace)
    vmis = _resource(k3s, "get", "vmi", "-n", namespace)["items"]
    vmi = next((item for item in vmis if _owner_uid(item, sandbox_uid)), None)
    if vmi is None:
        # The VMI is owned by its VM; the VM itself is owned by the Sandbox.
        vms = _resource(k3s, "get", "vm", "-n", namespace)["items"]
        vm = next((item for item in vms if _owner_uid(item, sandbox_uid)), None)
        if vm is None:
            raise AssertionError(f"no VM owned by Sandbox {sandbox_name} ({sandbox_uid})")
        vmi = next((item for item in vmis if item["metadata"]["name"] == vm["metadata"]["name"]), None)
    if vmi is None:
        raise AssertionError(f"no VMI found for Sandbox {sandbox_name} ({sandbox_uid})")
    launcher = _launcher_for_vmi(k3s, namespace, vmi)
    volumes = vmi.get("spec", {}).get("volumes", [])
    matching_pvs = [
        item
        for item in _resource(k3s, "get", "pv")["items"]
        if item.get("spec", {}).get("local", {}).get("path") == f"/dev/loop{loop_index}"
        and item.get("spec", {}).get("claimRef", {}).get("namespace") == namespace
    ]
    if len(matching_pvs) != 1:
        raise AssertionError(f"expected the VM's raw loop PV for /dev/loop{loop_index}, got {len(matching_pvs)}")
    pv = matching_pvs[0]
    claim_name = pv["spec"]["claimRef"]["name"]
    attached = any(volume.get("persistentVolumeClaim", {}).get("claimName") == claim_name for volume in volumes)
    task_processes = _container_tasks()
    container_statuses = launcher.get("status", {}).get("containerStatuses", [])
    launcher_ids = {
        status.get("containerID", "").removeprefix("containerd://")
        for status in container_statuses
        if status.get("containerID", "").startswith("containerd://")
    }
    launcher_tasks = {
        container_id: task_processes[container_id] for container_id in launcher_ids if container_id in task_processes
    }
    launcher_pids = {_task_pid(task) for task in launcher_tasks.values()}
    block_users = _block_device_users(loop_index)
    vm_block_users = {
        pid: user for pid, user in block_users.items() if launcher_pids.intersection(user["ancestor_pids"])
    }
    identity = marker["identity"]
    k3s_pid = int(identity["pid"])
    return {
        "sandbox": {
            "name": sandbox_name,
            "uid": sandbox_uid,
            "ready_condition": next(
                (
                    condition
                    for condition in sandbox.get("status", {}).get("conditions", [])
                    if condition.get("type") == "Ready"
                ),
                None,
            ),
        },
        "vmi": {
            "name": vmi["metadata"]["name"],
            "uid": vmi["metadata"]["uid"],
            "phase": vmi.get("status", {}).get("phase"),
            "attached_workspace_claim": claim_name if attached else None,
        },
        "launcher": {
            "name": launcher["metadata"]["name"],
            "uid": launcher["metadata"]["uid"],
            "phase": launcher.get("status", {}).get("phase"),
            "container_ids": sorted(launcher_ids),
            "task_processes": launcher_tasks,
        },
        "raw_volume": {
            "pv": pv["metadata"]["name"],
            "pvc": claim_name,
            "device": f"/dev/loop{loop_index}",
            "backing_file": _loop_backing(loop_index),
            "expected_backing_file": backing_file,
            "open_device_users": block_users,
            "vm_process_users": vm_block_users,
            "kernel_dependent_loops": _dependent_loop_mappings(os.fstat(device_fd).st_rdev),
            "fixture_owner_pid": os.getpid(),
        },
        "k3s": {
            "pid": k3s_pid,
            "start_ticks": identity["process_start_ticks"],
            "current_start_ticks": baseline._baseline_process_start(k3s_pid),
        },
        "container_tasks": task_processes,
    }


def test_retained_failure_cleanup(
    stack_inputs: StackInputs, runtime_factory: Any, scripted_listener: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject one caught error after a real Sandbox owns its block disk, then qualify teardown."""
    outputs = undeclared_outputs_dir()
    evidence: dict[str, Any] = {
        "probe": "retained-baseline-failure-cleanup-v1",
        "source_stack_sha256": hashlib.sha256(Path(stack_module.__file__).read_bytes()).hexdigest(),
        "test_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "fixture_owner_pid": os.getpid(),
        "observed_stop_owned_calls": 0,
        "outcome": "started",
    }
    evidence_path = outputs / "retained-failure-cleanup.json"
    runtime_holder: dict[str, Any] = {}

    def own_runtime(work: Path) -> Any:
        capabilities = runtime_factory(work)
        runtime_holder["capabilities"] = capabilities

        storage = capabilities.workspace_storage._storage
        original_close = storage.close

        def observe_storage_close() -> None:
            devices = storage.devices
            if len(devices) == 1 and devices[0].device_fd >= 0:
                device = devices[0]
                owned_device = os.fstat(device.device_fd).st_rdev
                teardown: dict[str, Any] = {"owned_block_device": owned_device}
                evidence["storage_detach"] = teardown
                try:
                    teardown["kernel_dependent_loops_before"] = _dependent_loop_mappings(owned_device)
                except BaseException as error:
                    teardown["before_capture_error"] = f"{type(error).__name__}: {error}"
                try:
                    original_close()
                finally:
                    try:
                        teardown["kernel_dependent_loops_after"] = _dependent_loop_mappings(owned_device)
                    except BaseException as error:
                        teardown["after_capture_error"] = f"{type(error).__name__}: {error}"
                    _save(evidence_path, evidence)
            else:
                original_close()

        storage.close = observe_storage_close
        return capabilities

    original_stop_owned = baseline.stop_owned

    def observe_stop_owned(k3s: baseline.K3s, marker: dict[str, Any]) -> None:
        evidence["observed_stop_owned_calls"] += 1
        frames = inspect.stack()
        evidence["guard_call_stack"] = [
            {"function": frame.function, "filename": Path(frame.filename).name, "line": frame.lineno}
            for frame in frames[:8]
        ]
        evidence["ownership_marker"] = marker
        capabilities = runtime_holder.get("capabilities")
        devices = capabilities.workspace_storage._storage.devices if capabilities is not None else []
        if len(devices) != 1:
            evidence["boundary_capture_error"] = f"expected one owned workspace loop device, got {len(devices)}"
        else:
            device = devices[0]
            try:
                evidence["before_stop_owned"] = _run_snapshot(
                    k3s,
                    marker,
                    stack_inputs.fixture.namespace,
                    evidence["sandbox"]["name"],
                    evidence["sandbox"]["uid"],
                    device.index,
                    str(device.backing_file),
                    device.device_fd,
                )
            except BaseException as error:
                evidence["boundary_capture_error"] = f"{type(error).__name__}: {error}"
            evidence["loop_at_guard_entry"] = _loop_backing(device.index)

        try:
            original_stop_owned(k3s, marker)
        except BaseException as error:
            evidence["real_stop_owned_error"] = f"{type(error).__name__}: {error}"
            raise
        else:
            evidence["real_stop_owned_returned"] = True
        finally:
            identity = marker["identity"]
            pid = int(identity["pid"])
            previous_tasks = evidence.get("before_stop_owned", {}).get("container_tasks", {})
            evidence["after_stop_owned"] = {
                "k3s_pid": pid,
                "k3s_start_ticks": baseline._baseline_process_start(pid),
                "container_tasks": _container_tasks(),
                "previous_task_processes": {
                    container_id: {**task, "current_start_ticks": baseline._baseline_process_start(_task_pid(task))}
                    for container_id, task in previous_tasks.items()
                },
                "previous_block_device_users": {
                    pid: {**user, "current_start_ticks": baseline._baseline_process_start(int(user["pid"]))}
                    for pid, user in evidence.get("before_stop_owned", {})
                    .get("raw_volume", {})
                    .get("open_device_users", {})
                    .items()
                },
                "previous_vm_block_device_users": {
                    pid: {**user, "current_start_ticks": baseline._baseline_process_start(int(user["pid"]))}
                    for pid, user in evidence.get("before_stop_owned", {})
                    .get("raw_volume", {})
                    .get("vm_process_users", {})
                    .items()
                },
                "current_block_device_users": (_block_device_users(devices[0].index) if len(devices) == 1 else {}),
                "loop_backing": _loop_backing(devices[0].index) if len(devices) == 1 else None,
                "kernel_dependent_loops": (
                    _dependent_loop_mappings(os.fstat(devices[0].device_fd).st_rdev)
                    if len(devices) == 1 and devices[0].device_fd >= 0
                    else None
                ),
            }
            _save(evidence_path, evidence)

    sentinel = _InjectedFailureError("qualification sentinel: failure after VM and raw disk are live")
    caught: BaseException | None = None
    try:
        with running_stack(
            stack_inputs,
            models=rendered_fixture.LocalModel(url=f"http://{scripted_listener.host}:{scripted_listener.port}"),
            validate_vm_shutdown=lambda: False,
            persist_infrastructure=os.environ.get("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE") == "1",
            runtime_factory=own_runtime,
        ) as stack:
            # Install only after setup; a warm-baseline handoff during open_baseline is outside this probe.
            monkeypatch.setattr(baseline, "stop_owned", observe_stop_owned)
            sandbox_name = f"cleanup-qualification-{uuid4().hex[:10]}"
            namespace = stack_inputs.fixture.namespace
            identity_body = {
                "apiVersion": IDENTITY_API_VERSION,
                "kind": "Identity",
                "metadata": {"name": sandbox_name, "namespace": namespace},
                "spec": {"description": f"Disposable cleanup qualification for {sandbox_name}."},
            }
            identity = json.loads(
                stack.k3s.kubectl("create", "-f", "-", "-o", "json", input_text=json.dumps(identity_body))
            )
            identity_uid = identity["metadata"]["uid"]
            if LAUNCH_DEFAULT_POLICY_SETS:
                binding = {
                    "apiVersion": IDENTITY_API_VERSION,
                    "kind": "PolicyBinding",
                    "metadata": {
                        "name": f"{sandbox_name}.identity",
                        "namespace": namespace,
                        "ownerReferences": [
                            {
                                "apiVersion": IDENTITY_API_VERSION,
                                "kind": "Identity",
                                "name": sandbox_name,
                                "uid": identity_uid,
                            }
                        ],
                    },
                    "spec": {
                        "subject": {"identity": {"name": sandbox_name}},
                        "policySets": sorted(LAUNCH_DEFAULT_POLICY_SETS),
                    },
                }
                stack.k3s.kubectl("create", "-f", "-", input_text=json.dumps(binding))
            sandbox_body = {
                "apiVersion": SANDBOX_API_VERSION,
                "kind": "Sandbox",
                "metadata": {"name": sandbox_name, "namespace": namespace},
                "spec": {"template": SUPERVISION_TEMPLATE, "running": True, "identity": {"name": sandbox_name}},
            }
            created = json.loads(
                stack.k3s.kubectl("create", "-f", "-", "-o", "json", input_text=json.dumps(sandbox_body))
            )
            evidence["sandbox"] = {"name": sandbox_name, "uid": created["metadata"]["uid"]}
            _save(evidence_path, evidence)
            stack.k3s.kubectl(
                "wait", f"sandbox/{sandbox_name}", "--for=condition=Ready", "--timeout=300s", "-n", namespace
            )
            capabilities = runtime_holder["capabilities"]
            devices = capabilities.workspace_storage._storage.devices
            assert len(devices) == 1, f"expected one raw workspace loop, got {len(devices)}"
            device = devices[0]
            pre = _run_snapshot(
                stack.k3s,
                json.loads(baseline._BASELINE_MARKER.read_text()),
                namespace,
                sandbox_name,
                created["metadata"]["uid"],
                device.index,
                str(device.backing_file),
                device.device_fd,
            )
            evidence["pre_injection"] = pre
            if pre["vmi"]["phase"] != "Running":
                raise AssertionError(f"VMI is not Running: {pre['vmi']}")
            if pre["launcher"]["phase"] != "Running":
                raise AssertionError(f"virt-launcher is not Running: {pre['launcher']}")
            if pre["vmi"]["attached_workspace_claim"] is None:
                raise AssertionError("running VMI does not attach the loop-backed workspace claim")
            if pre["raw_volume"]["backing_file"] != str(device.backing_file):
                raise AssertionError(f"workspace loop backing mismatch: {pre['raw_volume']}")
            if not pre["launcher"]["task_processes"]:
                raise AssertionError("virt-launcher container has no identifiable live containerd task process")
            if not pre["raw_volume"]["open_device_users"]:
                raise AssertionError("no live process holds the VM's exact raw loop block device open")
            if not pre["raw_volume"]["vm_process_users"]:
                raise AssertionError("no virt-launcher descendant holds the VM's exact raw loop block device open")
            if not pre["raw_volume"]["kernel_dependent_loops"]:
                raise AssertionError("no kernel loop mapping leases the VM's exact raw loop block device")
            evidence["injection"] = {"error_type": type(sentinel).__name__, "message": str(sentinel)}
            _save(evidence_path, evidence)
            raise sentinel
    except BaseException as error:
        caught = error
    finally:
        capabilities = runtime_holder.get("capabilities")
        devices = capabilities.workspace_storage._storage.devices if capabilities is not None else []
        evidence["final_loop_mappings"] = [
            {"index": device.index, "backing_file": _loop_backing(device.index), "expected": str(device.backing_file)}
            for device in devices
        ]
        evidence["final_kernel_dependent_loops"] = evidence.get("storage_detach", {}).get(
            "kernel_dependent_loops_after"
        )
        evidence["original_error_identity_preserved"] = caught is sentinel
        evidence["caught_error"] = f"{type(caught).__name__}: {caught}" if caught is not None else None
        evidence["outcome"] = "checks-pending" if caught is sentinel else "failed"
        workspace_root = baseline._executor_action_workspace_root()
        invalidation_marker = workspace_root / baseline._INVALIDATE_SNAPSHOT_MARKER if workspace_root else None
        evidence["runner_invalidation"] = {
            "workspace_root": str(workspace_root) if workspace_root else None,
            "marker": str(invalidation_marker) if invalidation_marker else None,
            "exists": invalidation_marker.is_file() if invalidation_marker else False,
        }
        _save(evidence_path, evidence)

    try:
        assert caught is sentinel, f"running_stack did not preserve the injected error: {caught!r}"
        assert evidence["observed_stop_owned_calls"] == 1, evidence
        assert "_stop_retained_baseline_on_error" in {frame["function"] for frame in evidence["guard_call_stack"]}, (
            evidence
        )
        assert "stack.py" in {frame["filename"] for frame in evidence["guard_call_stack"]}, evidence
        assert "boundary_capture_error" not in evidence, evidence
        boundary = evidence["before_stop_owned"]
        assert boundary["vmi"]["phase"] == "Running", boundary
        assert boundary["launcher"]["phase"] == "Running", boundary
        assert boundary["vmi"]["attached_workspace_claim"], boundary
        assert boundary["raw_volume"]["backing_file"] == boundary["raw_volume"]["expected_backing_file"], boundary
        assert boundary["launcher"]["task_processes"], boundary
        assert boundary["raw_volume"]["open_device_users"], boundary
        assert boundary["raw_volume"]["vm_process_users"], boundary
        after = evidence["after_stop_owned"]
        assert evidence["real_stop_owned_returned"] is True, evidence
        assert after["k3s_start_ticks"] is None, after
        assert after["container_tasks"] == {}, after
        assert all(
            task["current_start_ticks"] != task["start_ticks"] for task in after["previous_task_processes"].values()
        ), after
        assert all(
            user["current_start_ticks"] != user["start_ticks"] for user in after["previous_block_device_users"].values()
        ), after
        assert all(
            user["current_start_ticks"] != user["start_ticks"]
            for user in after["previous_vm_block_device_users"].values()
        ), after
        assert after["current_block_device_users"] == {}, after
        assert after["loop_backing"] == boundary["raw_volume"]["expected_backing_file"], after
        storage_detach = evidence["storage_detach"]
        assert storage_detach["kernel_dependent_loops_before"], storage_detach
        assert "before_capture_error" not in storage_detach, storage_detach
        assert "after_capture_error" not in storage_detach, storage_detach
        assert storage_detach["kernel_dependent_loops_after"] == [], storage_detach
        assert evidence["runner_invalidation"]["exists"] is True, evidence
        assert evidence["final_loop_mappings"] == [
            {
                "index": int(boundary["raw_volume"]["device"].removeprefix("/dev/loop")),
                "backing_file": "",
                "expected": boundary["raw_volume"]["expected_backing_file"],
            }
        ], evidence
    except BaseException as error:
        evidence["outcome"] = "failed"
        evidence["qualification_error"] = f"{type(error).__name__}: {error}"
        _save(evidence_path, evidence)
        raise
    evidence["outcome"] = "passed"
    _save(evidence_path, evidence)


if __name__ == "__main__":
    pytest_bazel.main()
