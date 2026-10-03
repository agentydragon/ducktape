"""Create and exercise one disposable Agentplane runner VM through its real inventory."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from uuid import uuid4

from kubernetes_asyncio import client as k8s_client, config as k8s_config
from pydantic import ValidationError

from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import RunnerClient
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubevirt import VmiResource, VmTemplate, controller_owned_by
from agentplane.sandbox_service.kubevirt_contract import (
    KUBEVIRT_API_VERSION,
    VM_KIND,
    VM_TEMPLATE_ANNOTATION,
    VM_TEMPLATE_CONFIG_ANNOTATION,
    VMIS_PLURAL,
)
from agentplane.sandbox_service.protocol_pb2 import CreateSandboxRequest, Sandbox
from util.kubernetes import CustomObjectsClient

_NAMESPACE_PREFIX = "agentplane-vm-prototype-"
_SESSION_ROOT = "/workspace/agentplane-vm-acceptance"
_SETUP_MARKERS = {
    "MEMORY_MAX_BYTES": re.compile(r"(?:max|[0-9]+)"),
    "MEMORY_CURRENT_BYTES": re.compile(r"[0-9]+"),
    "MEMORY_OOM_KILLS": re.compile(r"[0-9]+"),
    "CPU_MAX": re.compile(r"(?:max|[0-9]+) [0-9]+"),
    "PIDS_MAX": re.compile(r"(?:max|[0-9]+)"),
    "NATIVE_HISTORY_QUOTA_BYTES": re.compile(r"[0-9]+"),
    "NATIVE_HISTORY_USED_BYTES": re.compile(r"[0-9]+"),
    "WORKSPACE_QUOTA_BYTES": re.compile(r"[0-9]+"),
    "WORKSPACE_USED_BYTES": re.compile(r"[0-9]+"),
    "QUOTA_RESULT": re.compile(r"EDQUOT|ENOSPC|OK"),
    "WORKSPACE_SHA256": re.compile(r"[a-f0-9]{64}"),
}
_PROBE_SCRIPT = f"""\
set -eu
test "$(id -u)" = 1000
test "$(id -g)" = 1000
printf 'UID=%s\\n' "$(id -u)"
printf 'GID=%s\\n' "$(id -g)"
cgroup_path=$(awk -F: '$1 == "0" {{print $3}}' /proc/self/cgroup)
test -n "$cgroup_path"
printf 'CGROUP=%s\\n' "$cgroup_path"
test -d /state/sessions
if (umask 077; : > /state/sessions/.agentplane-vm-acceptance) 2>/dev/null; then
  rm -f /state/sessions/.agentplane-vm-acceptance
  printf 'STATE_SESSIONS=writeable\\n'
  exit 41
fi
printf 'STATE_SESSIONS=denied\\n'
memory_control="/sys/fs/cgroup$cgroup_path/memory.max"
if test -w "$memory_control"; then
  printf 'CGROUP_MEMORY_MAX=writeable\\n'
  exit 42
fi
printf 'CGROUP_MEMORY_MAX=denied\\n'
mkdir -p {_SESSION_ROOT}
printf 'agentplane-vm-acceptance-v1\\n' > {_SESSION_ROOT}/probe.txt
sha256sum {_SESSION_ROOT}/probe.txt | awk '{{print "WORKSPACE_SHA256=" $1}}'
"""


@asynccontextmanager
async def _inventory(
    namespace: str,
    *,
    kubeconfig: Path | None,
    vm_templates: dict[str, VmTemplate] | None = None,
    image_replacement: tuple[str, str, str, str] | None = None,
    timeout_s: float = 60,
) -> AsyncIterator[tuple[SandboxInventory, k8s_client.CoreV1Api, CustomObjectsClient]]:
    if not namespace.startswith(_NAMESPACE_PREFIX):
        raise ValueError(f"namespace must start with {_NAMESPACE_PREFIX!r}")
    configuration = k8s_client.Configuration()
    await k8s_config.load_kube_config(
        **({"config_file": str(kubeconfig)} if kubeconfig is not None else {}), client_configuration=configuration
    )
    async with k8s_client.ApiClient(configuration) as api:
        core = k8s_client.CoreV1Api(api)
        custom_objects = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api))
        templates = vm_templates or {}
        if image_replacement is not None:
            name, uid, template_name, image = image_replacement
            resource = await asyncio.wait_for(
                custom_objects.get_namespaced_custom_object("kubevirt.io", "v1", namespace, "virtualmachines", name),
                timeout=timeout_s,
            )
            metadata = resource.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("VM metadata is invalid")
            annotations = metadata.get("annotations", {})
            if not isinstance(annotations, dict):
                raise ValueError("VM annotations are invalid")
            if metadata.get("uid") != uid:
                raise ValueError(f"VM {name!r} UID does not match --uid")
            if annotations.get(VM_TEMPLATE_ANNOTATION) != template_name:
                raise ValueError("VM does not use the requested selected template")
            recorded_config = annotations.get(VM_TEMPLATE_CONFIG_ANNOTATION)
            if not isinstance(recorded_config, str):
                raise ValueError("VM has no recorded approved template configuration")
            try:
                recorded_values = json.loads(recorded_config)
            except ValueError:
                raise ValueError("VM recorded template configuration is invalid") from None
            if not isinstance(recorded_values, dict):
                raise ValueError("VM recorded template configuration is invalid")
            recorded_values["image"] = image
            templates = {**templates, template_name: _validated_template(recorded_values)}
        inventory = SandboxInventory(
            namespace=namespace, core_v1=core, custom_objects=custom_objects, vm_templates=templates
        )
        yield inventory, core, custom_objects


def _ready(pod: k8s_client.V1Pod) -> bool:
    return bool(
        pod.status
        and pod.status.phase == "Running"
        and any(condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions or [])
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("acceptance operation exceeded its deadline")
    return remaining


async def _within[T](awaitable: Awaitable[T], deadline: float) -> T:
    return await asyncio.wait_for(awaitable, timeout=_remaining(deadline))


async def _launcher_pod(
    inventory: SandboxInventory, core: k8s_client.CoreV1Api, name: str, uid: str
) -> k8s_client.V1Pod | None:
    vmi = await inventory.current_vm_instance(name, uid)
    if vmi is None:
        return None
    pods = await core.list_namespaced_pod(
        inventory.namespace, label_selector=f"kubevirt.io/created-by={vmi.metadata.uid}"
    )
    matching = [
        pod
        for pod in pods.items
        if pod.metadata is not None
        and _ready(pod)
        and len(controllers := [owner for owner in pod.metadata.owner_references or [] if owner.controller]) == 1
        and controllers[0].api_version == "kubevirt.io/v1"
        and controllers[0].kind == "VirtualMachineInstance"
        and controllers[0].name == vmi.metadata.name
        and controllers[0].uid == vmi.metadata.uid
    ]
    if len(matching) > 1:
        raise RuntimeError(f"VM {name!r} has more than one ready launcher Pod")
    return matching[0] if matching else None


async def _wait_ready(
    inventory: SandboxInventory, core: k8s_client.CoreV1Api, name: str, *, timeout_s: float
) -> tuple[Sandbox, k8s_client.V1Pod]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_state = "not observed"
    while asyncio.get_running_loop().time() < deadline:
        sandbox = await _within(inventory.get(name, kind="kubevirt"), deadline)
        last_state = sandbox.state
        if sandbox.state == "running":
            pod = await _within(_launcher_pod(inventory, core, name, sandbox.uid), deadline)
            if pod is not None:
                return sandbox, pod
        await asyncio.sleep(min(5, _remaining(deadline)))
    raise TimeoutError(f"VM {name!r} did not reach guest readiness within {timeout_s}s; last_state={last_state!r}")


async def _finish_provisioning(inventory: SandboxInventory, name: str, uid: str, *, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        sandbox = await _within(inventory.get(name, kind="kubevirt"), deadline)
        if sandbox.uid != uid:
            raise RuntimeError(f"VM {name!r} changed UID while provisioning")
        try:
            await _within(inventory.finish_provisioning(sandbox), deadline)
            return
        except k8s_client.ApiException as error:
            if error.status != 409:
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"VM {name!r} kept conflicting while provisioning") from error
            await asyncio.sleep(min(2, _remaining(deadline)))


async def _owned_vmi(custom_objects: CustomObjectsClient, namespace: str, name: str, vm_uid: str) -> VmiResource | None:
    group, version = KUBEVIRT_API_VERSION.split("/", maxsplit=1)
    try:
        raw = await custom_objects.get_namespaced_custom_object(group, version, namespace, VMIS_PLURAL, name)
    except k8s_client.ApiException as error:
        if error.status == 404:
            return None
        raise
    vmi = VmiResource.model_validate(raw)
    if not controller_owned_by(vmi.metadata, api_version=KUBEVIRT_API_VERSION, kind=VM_KIND, name=name, uid=vm_uid):
        raise RuntimeError(f"VMI {name!r} is not owned by VM UID {vm_uid}")
    return vmi


async def _wait_vmi_absent(
    custom_objects: CustomObjectsClient, namespace: str, name: str, vm_uid: str, *, deadline: float
) -> None:
    while asyncio.get_running_loop().time() < deadline:
        if await _within(_owned_vmi(custom_objects, namespace, name, vm_uid), deadline) is None:
            return
        await asyncio.sleep(min(2, _remaining(deadline)))
    raise TimeoutError(f"VMI for {name!r} did not disappear within the remaining deadline")


async def _resume_with_retry(inventory: SandboxInventory, name: str, uid: str, *, deadline: float) -> None:
    while True:
        try:
            await _within(inventory.resume(name, uid=uid, kind="kubevirt"), deadline)
            return
        except k8s_client.ApiException as error:
            if error.status != 409:
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"VM {name!r} kept conflicting while resuming") from error
            await asyncio.sleep(min(2, _remaining(deadline)))


def _port_forward(namespace: str, pod: k8s_client.V1Pod) -> str:
    if pod.metadata is None:
        raise RuntimeError("ready launcher Pod has no metadata")
    return f"kubectl -n {namespace} port-forward pod/{pod.metadata.name} 17000:7000"


def _node_selector(value: str) -> tuple[str, str]:
    key, separator, selected = value.partition("=")
    if not separator or not key or not selected:
        raise argparse.ArgumentTypeError("expected KEY=VALUE")
    return key, selected


def _context_window(value: str) -> tuple[str, int]:
    model, separator, size = value.partition("=")
    if not separator or not model or not size.isdecimal() or int(size) <= 0:
        raise argparse.ArgumentTypeError("expected MODEL=POSITIVE_INTEGER")
    return model, int(size)


def _localhost_target(value: str) -> str:
    host, separator, port = value.rpartition(":")
    if not separator or host not in {"localhost", "127.0.0.1", "[::1]"}:
        raise argparse.ArgumentTypeError("runner RPC must use a localhost kubectl port-forward")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise argparse.ArgumentTypeError("runner RPC port must be between 1 and 65535")
    return value


def _template(args: argparse.Namespace) -> VmTemplate:
    values = {
        "image": args.image,
        "image_pull_secret": args.image_pull_secret,
        "cpu_cores": args.cpu_cores,
        "memory": args.memory,
        "state_disk": args.state_disk,
        "workspace_disk": args.workspace_disk,
        "storage_class": args.storage_class,
        "node_selector": dict(args.node_selector),
        "llm_base_url": args.llm_base_url,
        "proxy_url": args.proxy_url,
        "model_context_windows": dict(args.model_context_window),
        "ca_bundle": args.ca_bundle_file.read_text(),
        "kubernetes_host": args.kubernetes_host,
        "kubernetes_credential_name": args.kubernetes_credential_name,
    }
    return _validated_template(values)


def _validated_template(values: dict[str, object]) -> VmTemplate:
    try:
        return VmTemplate.model_validate(values)
    except ValidationError as error:
        # Pydantic's default exception includes supplied values, including the CA bundle.
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
        raise ValueError(f"invalid VM template: {details}") from None


async def create(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", args.name) or len(args.name) > 57:
        raise ValueError("--name must be a DNS slug of at most 57 characters")
    template = _template(args)
    async with _inventory(args.namespace, kubeconfig=args.kubeconfig, vm_templates={args.template: template}) as (
        inventory,
        core,
        _custom_objects,
    ):
        deadline = asyncio.get_running_loop().time() + args.timeout
        created = await _within(
            inventory.create(CreateSandboxRequest(slug=args.name, template=args.template, kind="kubevirt")), deadline
        )
        print(
            json.dumps(
                {
                    "action": "vm_created",
                    "namespace": args.namespace,
                    "name": created.name,
                    "vm_uid": created.uid,
                    "image": template.image,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        while True:
            if await _within(inventory.ensure_vm_dependencies(created), deadline):
                break
            await asyncio.sleep(min(5, _remaining(deadline)))
        await _finish_provisioning(inventory, created.name, created.uid, timeout_s=_remaining(deadline))
        sandbox, pod = await _wait_ready(inventory, core, created.name, timeout_s=_remaining(deadline))
        print(
            json.dumps(
                {
                    "action": "created",
                    "namespace": args.namespace,
                    "name": sandbox.name,
                    "vm_uid": sandbox.uid,
                    "vmi_uid": sandbox.vm.vmi_uid,
                    "launcher_pod": pod.metadata.name if pod.metadata is not None else None,
                    "image": template.image,
                    "state_disk": template.state_disk,
                    "workspace_disk": template.workspace_disk,
                    "port_forward": _port_forward(args.namespace, pod),
                },
                sort_keys=True,
            )
        )


async def inspect(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    async with _inventory(args.namespace, kubeconfig=args.kubeconfig) as (inventory, core, _custom_objects):
        sandbox = await _within(inventory.get(args.name, kind="kubevirt"), deadline)
        pod = await _within(_launcher_pod(inventory, core, args.name, sandbox.uid), deadline)
        result: dict[str, object] = {
            "action": "inspect",
            "namespace": args.namespace,
            "name": sandbox.name,
            "state": sandbox.state,
            "vm_uid": sandbox.uid,
            "vmi_uid": sandbox.vm.vmi_uid,
            "launcher_pod": pod.metadata.name if pod is not None and pod.metadata is not None else None,
            "port_forward": _port_forward(args.namespace, pod) if pod is not None else None,
        }
        if args.target is not None:
            runner = RunnerClient(args.target)
            try:
                summaries = await _within(runner.list_sessions(), deadline)
            finally:
                await runner.close()
            result["sessions"] = [
                {
                    "session_id": item.session_id,
                    "harness": protocol_pb2.Harness.Name(item.spec.harness),
                    "harness_state": protocol_pb2.HarnessState.Name(item.harness_state),
                    "last_cursor": item.last_cursor,
                }
                for item in summaries
            ]
        print(json.dumps(result, sort_keys=True))


async def provision(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    async with _inventory(args.namespace, kubeconfig=args.kubeconfig) as (inventory, core, _custom_objects):
        sandbox = await _within(inventory.get(args.name, kind="kubevirt"), deadline)
        if sandbox.uid != args.uid:
            raise ValueError(f"VM {args.name!r} UID does not match --uid")
        print(
            json.dumps(
                {
                    "action": "provisioning_existing_vm",
                    "namespace": args.namespace,
                    "name": sandbox.name,
                    "vm_uid": sandbox.uid,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        await _finish_provisioning(inventory, sandbox.name, sandbox.uid, timeout_s=_remaining(deadline))
        ready, pod = await _wait_ready(inventory, core, sandbox.name, timeout_s=_remaining(deadline))
        print(
            json.dumps(
                {
                    "action": "provisioned",
                    "namespace": args.namespace,
                    "name": ready.name,
                    "vm_uid": ready.uid,
                    "vmi_uid": ready.vm.vmi_uid,
                    "launcher_pod": pod.metadata.name if pod.metadata is not None else None,
                    "port_forward": _port_forward(args.namespace, pod),
                },
                sort_keys=True,
            )
        )


async def replace_image(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    image_replacement = (args.name, args.uid, args.template, args.image)
    async with _inventory(
        args.namespace, kubeconfig=args.kubeconfig, image_replacement=image_replacement, timeout_s=args.timeout
    ) as (inventory, _core, _custom_objects):
        while True:
            try:
                replaced = await _within(
                    inventory.replace_vm_image(args.name, uid=args.uid, template_name=args.template), deadline
                )
                break
            except k8s_client.ApiException as error:
                if error.status != 409:
                    raise
                await asyncio.sleep(min(1, _remaining(deadline)))
        print(
            json.dumps(
                {
                    "action": "image_replaced",
                    "namespace": args.namespace,
                    "name": replaced.name,
                    "vm_uid": replaced.uid,
                    "template": args.template,
                    "image": args.image,
                    "state": replaced.state,
                },
                sort_keys=True,
            )
        )


def _probe_output(events: list[protocol_pb2.InitializationEvent]) -> dict[str, str]:
    output = b"".join(
        event.output.data
        for event in events
        if event.HasField("output") and event.output.stream == protocol_pb2.INITIALIZATION_STREAM_STDOUT
    ).decode("utf-8")
    markers = dict(
        re.findall(r"^(UID|GID|CGROUP|STATE_SESSIONS|CGROUP_MEMORY_MAX|WORKSPACE_SHA256)=(.*)$", output, re.MULTILINE)
    )
    expected = {"UID": "1000", "GID": "1000", "STATE_SESSIONS": "denied", "CGROUP_MEMORY_MAX": "denied"}
    if any(markers.get(key) != value for key, value in expected.items()):
        raise RuntimeError("initialization probe did not prove the expected guest identity and write boundaries")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", markers.get("CGROUP", "")):
        raise RuntimeError("initialization probe did not report a valid cgroup path")
    if not re.fullmatch(r"[a-f0-9]{64}", markers.get("WORKSPACE_SHA256", "")):
        raise RuntimeError("initialization probe did not report the workspace checksum")
    return markers


def _safe_setup_markers(output: bytes) -> dict[str, str]:
    markers: dict[str, str] = {}
    for line in output.decode("utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        validator = _SETUP_MARKERS.get(key)
        if separator and validator is not None and validator.fullmatch(value):
            markers[key] = value
    return markers


async def initialize(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    runner = RunnerClient(args.target)
    try:
        sessions = await _within(runner.list_sessions(), deadline)

        async def collect() -> list[protocol_pb2.InitializationEvent]:
            return [event async for event in runner.initialize_events(_PROBE_SCRIPT)]

        events = await _within(collect(), deadline)
    finally:
        await runner.close()
    results = [event.result for event in events if event.HasField("result")]
    if not results or results[-1].exit_code != 0:
        exit_code = results[-1].exit_code if results else "missing"
        raise RuntimeError(f"guest initialization probe exited with {exit_code}")
    markers = _probe_output(events)
    async with _inventory(args.namespace, kubeconfig=args.kubeconfig) as (inventory, _core, _custom_objects):
        sandbox = await _within(inventory.get(args.name, kind="kubevirt"), deadline)
        await _within(inventory.retire_vm_disk_initialization(sandbox.name, sandbox.uid), deadline)
    print(
        json.dumps(
            {
                "action": "initialized",
                "namespace": args.namespace,
                "name": args.name,
                "sessions_before_probe": len(sessions),
                **markers,
                "format_authorization_retired": True,
            },
            sort_keys=True,
        )
    )


def _harness(value: str) -> protocol_pb2.Harness:
    return {"claude": protocol_pb2.HARNESS_CLAUDE, "codex": protocol_pb2.HARNESS_CODEX}[value]


def _turn_text(entries: list[event_log_pb2.EventEntry]) -> str:
    assistant_items = {
        entry.event.item_started.item_id
        for entry in entries
        if entry.event.WhichOneof("observation") == "item_started"
        and entry.event.item_started.kind == event_pb2.ITEM_KIND_ASSISTANT_TEXT
    }
    return "".join(
        entry.event.item_completed.text
        for entry in entries
        if entry.event.WhichOneof("observation") == "item_completed"
        and entry.event.item_completed.item_id in assistant_items
        and entry.event.item_completed.HasField("text")
    ).strip()


async def _run_turn(
    runner: RunnerClient,
    *,
    session_id: str,
    spec: protocol_pb2.SessionSpec,
    prompt: str,
    timeout_s: float,
    setup_script: str | None = None,
    after_cursor: int = 0,
) -> tuple[event_log_pb2.EventEntry, list[event_log_pb2.EventEntry]]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    attachment = await _within(
        runner.attach(session_id, spec=spec, setup_script=setup_script, after_cursor=after_cursor), deadline
    )
    async with asyncio.timeout(_remaining(deadline)):
        async with attachment:
            await attachment.until(
                lambda entry: entry.event.WhichOneof("observation") in {"harness_started", "harness_launch_failed"},
                timeout_s=_remaining(deadline),
            )
            if any(entry.event.WhichOneof("observation") == "harness_launch_failed" for entry in attachment.seen):
                raise RuntimeError("native harness failed to launch; inspect the guest runner journal")
            await _within(attachment.send(f"acceptance-{uuid4().hex[:16]}", prompt), deadline)
            completed = await attachment.until(
                lambda entry: entry.event.WhichOneof("observation") == "turn_completed", timeout_s=_remaining(deadline)
            )
            if completed.event.turn_completed.status != event_pb2.TURN_STATUS_COMPLETED:
                raise RuntimeError("native harness turn did not complete successfully")
            return completed, list(attachment.seen)


async def session(args: argparse.Namespace) -> None:
    session_id = args.session_id or f"vm-acceptance-{args.harness}-{uuid4().hex[:12]}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session_id):
        raise ValueError("--session-id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    marker = f"VM_RECOVERY_{uuid4().hex[:16]}"
    workdir = f"{_SESSION_ROOT}/sessions/{session_id}"
    setup_script = f"mkdir -p {workdir}"
    runner = RunnerClient(args.target, capture_history=True)
    try:
        spec = protocol_pb2.SessionSpec(
            harness=_harness(args.harness), cwd=workdir, model=args.model, reasoning_effort=args.reasoning_effort
        )
        _, entries = await _run_turn(
            runner,
            session_id=session_id,
            spec=spec,
            prompt=f"Remember this opaque recovery marker for the next turn: {marker}. Reply with exactly: MARKER_SAVED",
            timeout_s=args.timeout,
            setup_script=setup_script,
        )
        if _turn_text(entries) != "MARKER_SAVED":
            raise RuntimeError("native harness did not return the expected initial marker acknowledgement")
    finally:
        await runner.close()
    if not entries:
        raise RuntimeError("runner attached without event evidence")
    print(
        json.dumps(
            {
                "action": "session_started",
                "session_id": session_id,
                "harness": args.harness,
                "model": args.model,
                "recovery_marker": marker,
                "source_id": entries[-1].origin.source_id,
                "after_cursor": entries[-1].cursor,
            },
            sort_keys=True,
        )
    )


async def setup_probe(args: argparse.Namespace) -> None:
    session_id = args.session_id or f"vm-probe-{uuid4().hex[:16]}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session_id):
        raise ValueError("--session-id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    print(json.dumps({"action": "setup_probe_started", "session_id": session_id}, sort_keys=True), flush=True)
    script = args.setup_script_file.read_text()
    if not script or len(script.encode()) > 65_536:
        raise ValueError("setup script must contain 1..65536 UTF-8 bytes")
    workdir = f"{_SESSION_ROOT}/setup/{session_id}"
    deadline = asyncio.get_running_loop().time() + args.timeout
    runner = RunnerClient(args.target, capture_history=True)
    attachment = None
    stream_drained = False
    try:
        await _within(runner.list_sessions(), deadline)
        attachment = await _within(
            runner.attach(
                session_id,
                spec=protocol_pb2.SessionSpec(
                    harness=_harness(args.harness),
                    cwd=workdir,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                ),
                setup_script=script,
            ),
            deadline,
        )
        async with asyncio.timeout(_remaining(deadline)):
            finished = await attachment.until(
                lambda entry: entry.event.WhichOneof("observation") in {"setup_finished", "setup_interrupted"},
                timeout_s=_remaining(deadline),
            )
            terminal_kind = finished.event.WhichOneof("observation")
            exit_code = finished.event.setup_finished.exit_code if terminal_kind == "setup_finished" else None
            if terminal_kind == "setup_finished" and exit_code == 0:
                launch = await attachment.until(
                    lambda entry: entry.event.WhichOneof("observation") in {"harness_started", "harness_launch_failed"},
                    timeout_s=_remaining(deadline),
                )
                if launch.event.WhichOneof("observation") == "harness_started":
                    await _within(attachment.detach(), deadline)
                else:
                    await _within(attachment.drain_until_end(), deadline)
                    stream_drained = True
            else:
                # Failed and interrupted setup streams close on their own. Drain through EOF before
                # exiting so Attachment cleanup cannot race a Detach write with the server close.
                await _within(attachment.drain_until_end(), deadline)
                stream_drained = True
            if not stream_drained:
                await _within(attachment.drain_until_end(), deadline)
                stream_drained = True
            entries = list(attachment.seen)
        summaries = await _within(runner.list_sessions(), deadline)
    finally:
        if attachment is not None and not stream_drained:
            attachment.cancel()
        await runner.close()
    stdout = b"".join(
        entry.event.setup_output.stdout
        for entry in entries
        if entry.event.WhichOneof("observation") == "setup_output"
        and entry.event.setup_output.WhichOneof("stream") == "stdout"
    )
    stderr = b"".join(
        entry.event.setup_output.stderr
        for entry in entries
        if entry.event.WhichOneof("observation") == "setup_output"
        and entry.event.setup_output.WhichOneof("stream") == "stderr"
    )
    output = stdout + stderr
    markers = _safe_setup_markers(output)
    details = (
        f"session_id={session_id}, terminal={terminal_kind}, exit_code={exit_code}, "
        f"stdout_bytes={len(stdout)}, stderr_bytes={len(stderr)}, markers={markers}"
    )
    summary = next((item for item in summaries if item.session_id == session_id), None)
    if summary is None:
        raise RuntimeError(f"runner did not retain the setup probe session; {details}")
    if args.expect_output is not None and args.expect_output.encode() not in output:
        raise RuntimeError(f"setup probe did not emit the requested evidence marker; {details}")
    for expectation in args.expect_marker:
        key, separator, value = expectation.partition("=")
        validator = _SETUP_MARKERS.get(key)
        if not separator or validator is None or not validator.fullmatch(value):
            raise ValueError("--expect-marker must use an allowed marker and valid value")
        if markers.get(key) != value:
            raise RuntimeError(f"setup probe did not emit expected marker {key}; {details}")
    if args.expect_failure:
        if terminal_kind == "setup_interrupted":
            if summary.setup_state != protocol_pb2.SETUP_STATE_INTERRUPTED:
                raise RuntimeError(f"ListSessions did not report the interrupted setup probe; {details}")
        elif exit_code == 0:
            raise RuntimeError(f"setup probe was expected to fail but exited successfully; {details}")
    elif terminal_kind != "setup_finished":
        raise RuntimeError(f"setup probe was interrupted before it reported an exit status; {details}")
    elif exit_code != args.expected_exit_code:
        raise RuntimeError(f"setup probe exited with {exit_code}; expected {args.expected_exit_code}; {details}")
    expected_setup_state = (
        protocol_pb2.SETUP_STATE_INTERRUPTED
        if terminal_kind == "setup_interrupted"
        else protocol_pb2.SETUP_STATE_FAILED
        if exit_code
        else protocol_pb2.SETUP_STATE_SUCCEEDED
    )
    if summary.setup_state != expected_setup_state:
        raise RuntimeError(f"ListSessions did not report the setup probe's terminal state; {details}")
    print(
        json.dumps(
            {
                "action": "setup_probe_completed",
                "session_id": session_id,
                "harness": args.harness,
                "exit_code": exit_code,
                "terminal_kind": terminal_kind,
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
                "expected_output_found": args.expect_output is None or args.expect_output.encode() in output,
                "markers": markers,
                "runner_responsive": True,
                "journal_listed": True,
            },
            sort_keys=True,
        )
    )


async def stop_start(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    async with _inventory(args.namespace, kubeconfig=args.kubeconfig) as (inventory, core, custom_objects):
        before = await _within(inventory.get(args.name, kind="kubevirt"), deadline)
        old_vmi = await _within(_owned_vmi(custom_objects, args.namespace, args.name, before.uid), deadline)
        if old_vmi is None:
            raise RuntimeError(f"VM {args.name!r} must be running before the stop/start cycle")
        await _within(inventory.suspend(args.name, uid=before.uid, kind="kubevirt"), deadline)
        await _wait_vmi_absent(custom_objects, args.namespace, args.name, before.uid, deadline=deadline)
        await _resume_with_retry(inventory, args.name, before.uid, deadline=deadline)
        after, pod = await _wait_ready(inventory, core, args.name, timeout_s=_remaining(deadline))
        if after.vm.vmi_uid == old_vmi.metadata.uid:
            raise RuntimeError("stop/start returned the original VMI UID")
        print(
            json.dumps(
                {
                    "action": "stop_start_completed",
                    "namespace": args.namespace,
                    "name": args.name,
                    "old_vmi_uid": old_vmi.metadata.uid,
                    "new_vmi_uid": after.vm.vmi_uid,
                    "launcher_pod": pod.metadata.name if pod.metadata is not None else None,
                    "port_forward": _port_forward(args.namespace, pod),
                },
                sort_keys=True,
            )
        )


async def resume_vm(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    async with _inventory(args.namespace, kubeconfig=args.kubeconfig) as (inventory, core, custom_objects):
        before = await _within(inventory.get(args.name, kind="kubevirt"), deadline)
        if before.uid != args.uid:
            raise ValueError(f"VM {args.name!r} UID does not match --uid")
        current_vmi = await _within(_owned_vmi(custom_objects, args.namespace, args.name, before.uid), deadline)
        if current_vmi is not None:
            raise RuntimeError(f"VM {args.name!r} still has VMI {current_vmi.metadata.uid}")
        print(
            json.dumps(
                {"action": "resuming_vm", "namespace": args.namespace, "name": before.name, "vm_uid": before.uid},
                sort_keys=True,
            ),
            flush=True,
        )
        await _resume_with_retry(inventory, args.name, before.uid, deadline=deadline)
        ready, pod = await _wait_ready(inventory, core, args.name, timeout_s=_remaining(deadline))
        print(
            json.dumps(
                {
                    "action": "resumed",
                    "namespace": args.namespace,
                    "name": ready.name,
                    "vm_uid": ready.uid,
                    "vmi_uid": ready.vm.vmi_uid,
                    "launcher_pod": pod.metadata.name if pod.metadata is not None else None,
                    "port_forward": _port_forward(args.namespace, pod),
                },
                sort_keys=True,
            )
        )


async def recover(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    runner = RunnerClient(args.target, capture_history=True)
    try:
        summaries = await _within(runner.list_sessions(), deadline)
        summary = next((item for item in summaries if item.session_id == args.session_id), None)
        if summary is None:
            raise RuntimeError(f"runner does not list session {args.session_id!r}")
        if summary.last_cursor < args.after_cursor:
            raise RuntimeError("recovered runner cursor moved behind the pre-stop cursor")
        _, entries = await _run_turn(
            runner,
            session_id=args.session_id,
            spec=summary.spec,
            prompt="What opaque recovery marker did I ask you to remember? Reply with exactly that marker.",
            timeout_s=_remaining(deadline),
            after_cursor=args.after_cursor,
        )
    finally:
        await runner.close()
    if not entries:
        raise RuntimeError("runner returned no post-stop event evidence")
    source_ids = {entry.origin.source_id for entry in entries}
    if source_ids != {args.source_id}:
        raise RuntimeError("recovered session event source changed")
    cursors = [entry.cursor for entry in entries]
    if cursors != list(range(args.after_cursor + 1, args.after_cursor + len(cursors) + 1)):
        raise RuntimeError("recovered session event cursor is not contiguous")
    starts = [
        entry.event.harness_started for entry in entries if entry.event.WhichOneof("observation") == "harness_started"
    ]
    if not starts or not starts[-1].resumed:
        raise RuntimeError("native harness did not report continuation after the VM restart")
    if _turn_text(entries) != args.recovery_marker:
        raise RuntimeError("native session did not continue with the remembered recovery marker")
    print(
        json.dumps(
            {
                "action": "recovery_verified",
                "session_id": args.session_id,
                "source_id": args.source_id,
                "harness_resumed": True,
                "marker_recalled": True,
                "last_cursor": cursors[-1],
            },
            sort_keys=True,
        )
    )


def _add_kubeconfig(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kubeconfig", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create_parser = commands.add_parser("create", help="create and boot one fresh provider-managed VM")
    create_parser.add_argument("--namespace", required=True)
    create_parser.add_argument("--name", required=True, help="DNS slug; the provider appends a random suffix")
    create_parser.add_argument("--image", required=True, help="digest-pinned runner containerDisk image")
    create_parser.add_argument("--storage-class", required=True)
    create_parser.add_argument("--image-pull-secret", required=True)
    create_parser.add_argument("--llm-base-url", required=True)
    create_parser.add_argument("--proxy-url", required=True)
    create_parser.add_argument("--ca-bundle-file", required=True, type=Path)
    create_parser.add_argument("--kubernetes-host", required=True)
    create_parser.add_argument("--kubernetes-credential-name", required=True)
    create_parser.add_argument("--template", default="prototype")
    create_parser.add_argument("--cpu-cores", type=int, default=4)
    create_parser.add_argument("--memory", default="10Gi")
    create_parser.add_argument("--state-disk", default="20Gi")
    create_parser.add_argument("--workspace-disk", default="40Gi")
    create_parser.add_argument("--node-selector", action="append", type=_node_selector, default=[])
    create_parser.add_argument("--model-context-window", action="append", type=_context_window, default=[])
    create_parser.add_argument("--timeout", type=float, default=3600)
    _add_kubeconfig(create_parser)
    create_parser.set_defaults(handler=create)

    inspect_parser = commands.add_parser("inspect", help="inspect inventory state and optionally ListSessions")
    inspect_parser.add_argument("--namespace", required=True)
    inspect_parser.add_argument("--name", required=True)
    inspect_parser.add_argument("--target", type=_localhost_target)
    inspect_parser.add_argument("--timeout", type=float, default=60)
    _add_kubeconfig(inspect_parser)
    inspect_parser.set_defaults(handler=inspect)

    provision_parser = commands.add_parser("provision", help="retry dependency handoff for one known-created VM")
    provision_parser.add_argument("--namespace", required=True)
    provision_parser.add_argument("--name", required=True)
    provision_parser.add_argument("--uid", required=True, help="the VM UID printed by create")
    provision_parser.add_argument("--timeout", type=float, default=900)
    _add_kubeconfig(provision_parser)
    provision_parser.set_defaults(handler=provision)

    replace_parser = commands.add_parser(
        "replace-image", help="replace the root image for one halted VM through the selected provider template"
    )
    replace_parser.add_argument("--namespace", required=True)
    replace_parser.add_argument("--name", required=True)
    replace_parser.add_argument("--uid", required=True)
    replace_parser.add_argument("--template", default="prototype")
    replace_parser.add_argument("--image", required=True, help="new digest-pinned runner image")
    replace_parser.add_argument("--timeout", type=float, default=300)
    _add_kubeconfig(replace_parser)
    replace_parser.set_defaults(handler=replace_image)

    initialize_parser = commands.add_parser("initialize", help="ListSessions and run deterministic guest probes")
    initialize_parser.add_argument("--namespace", required=True)
    initialize_parser.add_argument("--name", required=True)
    initialize_parser.add_argument("--target", type=_localhost_target, required=True)
    initialize_parser.add_argument("--timeout", type=float, default=180)
    _add_kubeconfig(initialize_parser)
    initialize_parser.set_defaults(handler=initialize)

    session_parser = commands.add_parser("session", help="start a real native Claude or Codex turn")
    session_parser.add_argument("--target", type=_localhost_target, required=True)
    session_parser.add_argument("--harness", choices=("claude", "codex"), required=True)
    session_parser.add_argument("--model", required=True)
    session_parser.add_argument("--session-id")
    session_parser.add_argument("--reasoning-effort", default="low")
    session_parser.add_argument("--timeout", type=float, default=300)
    session_parser.set_defaults(handler=session)

    setup_parser = commands.add_parser("setup-probe", help="run a bounded per-session guest setup probe")
    setup_parser.add_argument("--target", type=_localhost_target, required=True)
    setup_parser.add_argument("--session-id")
    setup_parser.add_argument("--harness", choices=("claude", "codex"), required=True)
    setup_parser.add_argument("--model", required=True)
    setup_parser.add_argument("--reasoning-effort", default="low")
    setup_parser.add_argument("--setup-script-file", type=Path, required=True)
    expected = setup_parser.add_mutually_exclusive_group()
    expected.add_argument("--expected-exit-code", type=int, default=0)
    expected.add_argument("--expect-failure", action="store_true")
    setup_parser.add_argument("--expect-output")
    setup_parser.add_argument(
        "--expect-marker",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="require and record a safe guest marker (memory/cpu/pids/quota/workspace)",
    )
    setup_parser.add_argument("--timeout", type=float, default=300)
    setup_parser.set_defaults(handler=setup_probe)

    restart_parser = commands.add_parser("stop-start", help="suspend, wait for the old VMI, and resume")
    restart_parser.add_argument("--namespace", required=True)
    restart_parser.add_argument("--name", required=True)
    restart_parser.add_argument("--timeout", type=float, default=900)
    _add_kubeconfig(restart_parser)
    restart_parser.set_defaults(handler=stop_start)

    resume_parser = commands.add_parser("resume", help="resume a suspended VM through the provider")
    resume_parser.add_argument("--namespace", required=True)
    resume_parser.add_argument("--name", required=True)
    resume_parser.add_argument("--uid", required=True, help="the VM UID printed by create")
    resume_parser.add_argument("--timeout", type=float, default=900)
    _add_kubeconfig(resume_parser)
    resume_parser.set_defaults(handler=resume_vm)

    recover_parser = commands.add_parser("recover", help="verify native continuation after stop/start")
    recover_parser.add_argument("--target", type=_localhost_target, required=True)
    recover_parser.add_argument("--session-id", required=True)
    recover_parser.add_argument("--source-id", required=True)
    recover_parser.add_argument("--after-cursor", type=int, required=True)
    recover_parser.add_argument("--recovery-marker", required=True)
    recover_parser.add_argument("--timeout", type=float, default=300)
    recover_parser.set_defaults(handler=recover)
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    await args.handler(args)


def main() -> None:
    asyncio.run(_async_main(build_parser().parse_args()))


if __name__ == "__main__":
    main()
