"""Create and exercise one disposable Agentplane runner VM through direct KubeVirt APIs."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import uuid4

import grpc
from cdk8s import App, Chart
from kubernetes_asyncio import client as k8s_client, config as k8s_config

from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import RunnerClient
from cluster.cdk8s.agentplane.kubevirt_experiment.policy import MANAGED_LABEL, SERVICE_ACCOUNT_ANNOTATION
from cluster.cdk8s.agentplane.kubevirt_experiment.vm import runner_vm
from util.kubernetes import CustomObjectsClient

_NAMESPACE_PREFIX = "agentplane-vm-prototype-"
_KUBEVIRT_API_VERSION = "kubevirt.io/v1"
_VM_PLURAL = "virtualmachines"
_VMI_PLURAL = "virtualmachineinstances"
_TEMPLATE_ANNOTATION = "agentplane.allegedly.works/vm-template"
_DISK_OWNER_ANNOTATION = "agentplane.allegedly.works/environment-uid"
_FORMAT_CONFIG_KEY = "config.json"
_IMAGE_PULL_SECRET = "forgejo-images-creds"
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


class _ConfigMapMergePatchClient(Protocol):
    async def patch_namespaced_config_map(
        self, name: str, namespace: str, body: dict[str, Any], *, _content_type: str
    ) -> object: ...


@asynccontextmanager
async def _apis(
    namespace: str, *, kubeconfig: Path | None
) -> AsyncIterator[tuple[k8s_client.CoreV1Api, CustomObjectsClient, k8s_client.ApiClient]]:
    if not namespace.startswith(_NAMESPACE_PREFIX):
        raise ValueError(f"namespace must start with {_NAMESPACE_PREFIX!r}")
    configuration = k8s_client.Configuration()
    await k8s_config.load_kube_config(
        **({"config_file": str(kubeconfig)} if kubeconfig is not None else {}), client_configuration=configuration
    )
    async with k8s_client.ApiClient(configuration) as api:
        yield k8s_client.CoreV1Api(api), cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api)), api


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


def _metadata(resource: dict[str, Any], *, kind: str) -> dict[str, Any]:
    metadata = resource.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError(f"{kind} has invalid metadata")
    return metadata


def _vm_identity(resource: dict[str, Any]) -> tuple[str, str, str]:
    metadata = _metadata(resource, kind="VM")
    name, namespace, uid = metadata.get("name"), metadata.get("namespace"), metadata.get("uid")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(namespace, str)
        or not namespace
        or not isinstance(uid, str)
        or not uid
    ):
        raise RuntimeError("VM is missing name, namespace, or UID")
    return name, namespace, uid


def _resource_view(resource: dict[str, Any], *, kind: str) -> dict[str, Any]:
    metadata = _metadata(resource, kind=kind)
    identity = {key: metadata[key] for key in ("name", "namespace", "uid") if key in metadata}
    view: dict[str, Any] = {"identity": identity, "status": resource.get("status", {})}
    if kind == "VirtualMachine":
        view["runStrategy"] = resource.get("spec", {}).get("runStrategy")
    return view


async def _vm(custom: CustomObjectsClient, namespace: str, name: str) -> dict[str, Any]:
    resource = await custom.get_namespaced_custom_object("kubevirt.io", "v1", namespace, _VM_PLURAL, name)
    metadata = _metadata(resource, kind="VM")
    if metadata.get("namespace") != namespace or metadata.get("name") != name:
        raise RuntimeError("KubeVirt returned a VM outside the requested identity")
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    if not isinstance(labels, dict) or labels.get(MANAGED_LABEL) != "true":
        raise ValueError(f"VM {name!r} is not managed by this experiment")
    if not isinstance(annotations, dict) or annotations.get(_TEMPLATE_ANNOTATION) != "prototype":
        raise ValueError(f"VM {name!r} is not the experiment runner template")
    if annotations.get(SERVICE_ACCOUNT_ANNOTATION) != f"vm-{name}-account":
        raise ValueError(f"VM {name!r} has a different launcher ServiceAccount")
    return resource


async def _owned_vmi(custom: CustomObjectsClient, namespace: str, name: str, vm_uid: str) -> dict[str, Any] | None:
    try:
        vmi = await custom.get_namespaced_custom_object("kubevirt.io", "v1", namespace, _VMI_PLURAL, name)
    except k8s_client.ApiException as error:
        if error.status == 404:
            return None
        raise
    metadata = _metadata(vmi, kind="VMI")
    owners = metadata.get("ownerReferences", [])
    controllers = [item for item in owners if item.get("controller") is True]
    if (
        metadata.get("namespace") != namespace
        or metadata.get("name") != name
        or len(controllers) != 1
        or controllers[0].get("apiVersion") != _KUBEVIRT_API_VERSION
        or controllers[0].get("kind") != "VirtualMachine"
        or controllers[0].get("name") != name
        or controllers[0].get("uid") != vm_uid
    ):
        raise RuntimeError(f"VMI {name!r} is not owned by VM UID {vm_uid}")
    return vmi


async def _launcher_pod(
    core: k8s_client.CoreV1Api, namespace: str, name: str, vmi: dict[str, Any]
) -> k8s_client.V1Pod | None:
    vmi_metadata = _metadata(vmi, kind="VMI")
    vmi_uid = vmi_metadata["uid"]
    pods = await core.list_namespaced_pod(namespace, label_selector=f"kubevirt.io/created-by={vmi_uid}")
    matching = []
    for pod in pods.items:
        metadata = pod.metadata
        controllers = [owner for owner in metadata.owner_references or [] if owner.controller] if metadata else []
        if (
            metadata is not None
            and metadata.namespace == namespace
            and len(controllers) == 1
            and controllers[0].api_version == _KUBEVIRT_API_VERSION
            and controllers[0].kind == "VirtualMachineInstance"
            and controllers[0].name == name
            and controllers[0].uid == vmi_uid
        ):
            matching.append(pod)
    if len(matching) > 1:
        raise RuntimeError(f"VM {name!r} has more than one launcher Pod for VMI UID {vmi_uid}")
    return matching[0] if matching else None


async def _wait_ready(
    custom: CustomObjectsClient,
    core: k8s_client.CoreV1Api,
    api_client: k8s_client.ApiClient,
    namespace: str,
    name: str,
    uid: str,
    *,
    timeout_s: float,
) -> tuple[dict[str, Any], dict[str, Any], k8s_client.V1Pod]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_status: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        vm = await _within(_vm(custom, namespace, name), deadline)
        if _vm_identity(vm)[2] != uid:
            raise RuntimeError(f"VM {name!r} changed UID while waiting for readiness")
        vmi = await _within(_owned_vmi(custom, namespace, name, uid), deadline)
        if vmi is not None:
            pod = await _within(_launcher_pod(core, namespace, name, vmi), deadline)
            status = vmi.get("status", {})
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in status.get("conditions", [])
            )
            if (
                vm.get("spec", {}).get("runStrategy") == "Always"
                and status.get("phase") == "Running"
                and ready
                and pod is not None
                and _ready(pod)
            ):
                return vm, vmi, pod
            last_status = {
                "vm": _resource_view(vm, kind="VirtualMachine"),
                "vmi": _resource_view(vmi, kind="VirtualMachineInstance"),
                "pod": _pod_view(pod, api_client) if pod is not None else None,
            }
        else:
            last_status = {"vm": _resource_view(vm, kind="VirtualMachine"), "vmi": None, "pod": None}
        await asyncio.sleep(min(5, _remaining(deadline)))
    raise TimeoutError(f"VM {name!r} did not become ready within {timeout_s}s; last_status={last_status!r}")


def _pod_view(pod: k8s_client.V1Pod, api_client: k8s_client.ApiClient) -> dict[str, Any]:
    if pod.metadata is None:
        raise RuntimeError("launcher Pod has no metadata")
    identity = {"name": pod.metadata.name, "namespace": pod.metadata.namespace, "uid": pod.metadata.uid}
    status = api_client.sanitize_for_serialization(pod.status) if pod.status is not None else {}
    return {"identity": identity, "status": status}


async def _wait_vmi_absent(
    custom: CustomObjectsClient, namespace: str, name: str, vm_uid: str, *, deadline: float
) -> None:
    while asyncio.get_running_loop().time() < deadline:
        if await _within(_owned_vmi(custom, namespace, name, vm_uid), deadline) is None:
            return
        await asyncio.sleep(min(2, _remaining(deadline)))
    raise TimeoutError(f"VMI for {name!r} was not deleted before the deadline")


async def _patch_vm(
    custom: CustomObjectsClient,
    namespace: str,
    name: str,
    uid: str,
    patch: dict[str, Any],
    *,
    expected_resource_version: str | None = None,
) -> None:
    resource = await _vm(custom, namespace, name)
    metadata = _metadata(resource, kind="VM")
    if metadata.get("uid") != uid:
        raise ValueError(f"VM {name!r} UID does not match --uid")
    patch_metadata = patch.setdefault("metadata", {})
    patch_metadata.update({"uid": uid, "resourceVersion": expected_resource_version or metadata["resourceVersion"]})
    await custom.patch_namespaced_custom_object(
        "kubevirt.io", "v1", namespace, _VM_PLURAL, name, patch, _content_type="application/merge-patch+json"
    )


async def _resume_with_retry(
    custom: CustomObjectsClient, namespace: str, name: str, uid: str, *, deadline: float
) -> None:
    while True:
        resource = await _within(_vm(custom, namespace, name), deadline)
        if _vm_identity(resource)[2] != uid:
            raise ValueError(f"VM {name!r} UID does not match --uid")
        if await _within(_owned_vmi(custom, namespace, name, uid), deadline) is not None:
            raise RuntimeError(f"VM {name!r} still has a VMI; wait for its deletion before resume")
        try:
            await _within(_patch_vm(custom, namespace, name, uid, {"spec": {"runStrategy": "Always"}}), deadline)
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


def _origin(value: str, option: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{option} must be an HTTP(S) origin URL without credentials or a path")
    return value.rstrip("/")


def _validate_create_args(args: argparse.Namespace) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", args.name) or len(args.name) > 39:
        raise ValueError("--name must be a DNS slug of at most 39 characters")
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", args.image):
        raise ValueError("--image must be a digest-pinned image reference")
    if not args.kubernetes_host or any(character.isspace() for character in args.kubernetes_host):
        raise ValueError("--kubernetes-host must be a host name")
    if len(dict(args.node_selector)) != len(args.node_selector):
        raise ValueError("--node-selector keys must be unique")
    if len(dict(args.model_context_window)) != len(args.model_context_window):
        raise ValueError("--model-context-window model names must be unique")
    if args.cpu_cores < 1:
        raise ValueError("--cpu-cores must be positive")
    for option in ("memory", "state_disk", "workspace_disk", "storage_class"):
        if not getattr(args, option):
            raise ValueError(f"--{option.replace('_', '-')} must not be empty")
    return {
        "llm_base_url": _origin(args.llm_base_url, "--llm-base-url"),
        "proxy_url": _origin(args.proxy_url, "--proxy-url"),
        "node_selector": dict(args.node_selector),
        "model_context_windows": dict(args.model_context_window),
    }


def _aux_name(name: str, suffix: str) -> str:
    return f"vm-{name}-{suffix}"


def _data_volume(name: str, *, uid: str, size: str, storage_class: str) -> dict[str, Any]:
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {"name": name, "labels": {MANAGED_LABEL: "true"}, "annotations": {_DISK_OWNER_ANNOTATION: uid}},
        "spec": {
            "source": {"blank": {}},
            "pvc": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": storage_class,
                "resources": {"requests": {"storage": size}},
            },
        },
    }


async def _ensure_data_volume(
    custom: CustomObjectsClient, namespace: str, name: str, *, uid: str, size: str, storage_class: str
) -> None:
    expected = _data_volume(name, uid=uid, size=size, storage_class=storage_class)
    try:
        actual = await custom.create_namespaced_custom_object(
            "cdi.kubevirt.io", "v1beta1", namespace, "datavolumes", expected
        )
    except k8s_client.ApiException as error:
        if error.status != 409:
            raise
        actual = await custom.get_namespaced_custom_object("cdi.kubevirt.io", "v1beta1", namespace, "datavolumes", name)
    metadata = _metadata(actual, kind="DataVolume")
    spec = actual.get("spec", {})
    if (
        metadata.get("annotations", {}).get(_DISK_OWNER_ANNOTATION) != uid
        or spec.get("source") != {"blank": {}}
        or spec.get("pvc", {}).get("accessModes") != ["ReadWriteOnce"]
        or spec.get("pvc", {}).get("storageClassName") != storage_class
        or spec.get("pvc", {}).get("resources", {}).get("requests", {}).get("storage") != size
    ):
        raise ValueError(f"DataVolume {name!r} does not match this VM's requested blank disk")


def _config_data(args: argparse.Namespace, *, uid: str, config: dict[str, Any]) -> dict[str, str]:
    guest_config = {
        "version": 1,
        "environment_id": uid,
        "listen": "0.0.0.0:7000",
        "llm_base_url": config["llm_base_url"],
        "proxy_url": config["proxy_url"],
        "model_context_windows": config["model_context_windows"],
        "format_blank_disks": ["state", "workspace"],
    }
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "experiment",
                "cluster": {
                    "server": f"https://{args.kubernetes_host}",
                    "certificate-authority": "/run/agentplane/ca-certificates.crt",
                },
            }
        ],
        "users": [{"name": "experiment-no-auth", "user": {}}],
        "contexts": [{"name": "experiment", "context": {"cluster": "experiment", "user": "experiment-no-auth"}}],
        "current-context": "experiment",
    }
    return {
        _FORMAT_CONFIG_KEY: json.dumps(guest_config, separators=(",", ":")),
        "kubeconfig": json.dumps(kubeconfig, separators=(",", ":")),
    }


async def _ensure_config_map(
    core: k8s_client.CoreV1Api, namespace: str, vm_name: str, *, uid: str, data: dict[str, str]
) -> None:
    name = _aux_name(vm_name, "config")
    body = k8s_client.V1ConfigMap(
        metadata=k8s_client.V1ObjectMeta(
            name=name,
            owner_references=[
                k8s_client.V1OwnerReference(
                    api_version=_KUBEVIRT_API_VERSION,
                    kind="VirtualMachine",
                    name=vm_name,
                    uid=uid,
                    controller=False,
                    block_owner_deletion=False,
                )
            ],
        ),
        data=data,
    )
    try:
        actual = await core.create_namespaced_config_map(namespace, body)
    except k8s_client.ApiException as error:
        if error.status != 409:
            raise
        actual = await core.read_namespaced_config_map(name, namespace)
    owners = actual.metadata.owner_references if actual.metadata is not None else []
    if (
        dict(actual.data or {}) != data
        or len(owners or []) != 1
        or owners[0].api_version != _KUBEVIRT_API_VERSION
        or owners[0].kind != "VirtualMachine"
        or owners[0].name != vm_name
        or owners[0].uid != uid
    ):
        raise ValueError(f"ConfigMap {name!r} does not belong to this VM")


async def _check_data_volumes(
    custom: CustomObjectsClient,
    namespace: str,
    name: str,
    *,
    uid: str,
    sizes: dict[str, str],
    storage_class: str,
    deadline: float,
) -> None:
    for disk, size in sizes.items():
        volume_name = _aux_name(name, disk)
        volume = await _within(
            custom.get_namespaced_custom_object("cdi.kubevirt.io", "v1beta1", namespace, "datavolumes", volume_name),
            deadline,
        )
        metadata = _metadata(volume, kind="DataVolume")
        spec = volume.get("spec", {})
        pvc = spec.get("pvc", {})
        if (
            metadata.get("annotations", {}).get(_DISK_OWNER_ANNOTATION) != uid
            or spec.get("source") != {"blank": {}}
            or pvc.get("accessModes") != ["ReadWriteOnce"]
            or pvc.get("storageClassName") != storage_class
            or pvc.get("resources", {}).get("requests", {}).get("storage") != size
        ):
            raise ValueError(f"DataVolume {volume_name!r} changed while provisioning")
        phase = volume.get("status", {}).get("phase")
        if phase in {"Failed", "Unknown"}:
            raise ValueError(f"DataVolume {volume_name!r} failed initialization")


async def _check_inputs(core: k8s_client.CoreV1Api, namespace: str, args: argparse.Namespace) -> None:
    pull_secret = await core.read_namespaced_secret(args.image_pull_secret, namespace)
    if pull_secret.type != "kubernetes.io/dockerconfigjson":
        raise ValueError("image pull Secret has the wrong type")
    trust = await core.read_namespaced_config_map(args.ca_bundle_config_map, namespace)
    files = dict(trust.data or {}) | dict(trust.binary_data or {})
    required = {"ca-certificates.crt", "ca-certificates.p12"}
    if any(not files.get(key) for key in required):
        raise ValueError("trust ConfigMap must contain both CA bundle files")


async def create(args: argparse.Namespace) -> None:
    values = _validate_create_args(args)
    name = f"{args.name}-{uuid4().hex[:8]}"
    if len(name) > 63:
        raise ValueError("generated VM name exceeds Kubernetes DNS name limit")
    deadline = asyncio.get_running_loop().time() + args.timeout
    async with _apis(args.namespace, kubeconfig=args.kubeconfig) as (core, custom, api_client):
        await _within(_check_inputs(core, args.namespace, args), deadline)
        account = _aux_name(name, "account")
        await _within(
            core.create_namespaced_service_account(
                args.namespace,
                k8s_client.V1ServiceAccount(
                    metadata=k8s_client.V1ObjectMeta(
                        name=account, labels={MANAGED_LABEL: "true", "agentplane.allegedly.works/caller": "true"}
                    ),
                    image_pull_secrets=[k8s_client.V1LocalObjectReference(name=args.image_pull_secret)],
                ),
            ),
            deadline,
        )
        chart = Chart(App(), "runner-vm", disable_resource_name_hashes=True)
        rendered = runner_vm(
            chart,
            namespace=args.namespace,
            name=name,
            image=args.image,
            image_pull_secret=args.image_pull_secret,
            cpu_cores=args.cpu_cores,
            memory=args.memory,
            state_claim=_aux_name(name, "state"),
            workspace_claim=_aux_name(name, "workspace"),
            config_map=_aux_name(name, "config"),
            trust_config_map=args.ca_bundle_config_map,
            node_selector=values["node_selector"],
        ).to_json()
        created = await _within(
            custom.create_namespaced_custom_object("kubevirt.io", "v1", args.namespace, _VM_PLURAL, rendered), deadline
        )
        vm_name, vm_namespace, uid = _vm_identity(created)
        print(
            json.dumps(
                {"action": "vm_created", "identity": {"name": vm_name, "namespace": vm_namespace, "uid": uid}},
                sort_keys=True,
            ),
            flush=True,
        )
        await _within(
            core.patch_namespaced_service_account(
                account,
                args.namespace,
                {
                    "metadata": {
                        "ownerReferences": [
                            {
                                "apiVersion": _KUBEVIRT_API_VERSION,
                                "kind": "VirtualMachine",
                                "name": name,
                                "uid": uid,
                                "controller": True,
                                "blockOwnerDeletion": False,
                            }
                        ]
                    }
                },
            ),
            deadline,
        )
        for disk, size in (("state", args.state_disk), ("workspace", args.workspace_disk)):
            await _within(
                _ensure_data_volume(
                    custom, args.namespace, _aux_name(name, disk), uid=uid, size=size, storage_class=args.storage_class
                ),
                deadline,
            )
        await _within(
            _ensure_config_map(core, args.namespace, name, uid=uid, data=_config_data(args, uid=uid, config=values)),
            deadline,
        )
        await _check_data_volumes(
            custom,
            args.namespace,
            name,
            uid=uid,
            sizes={"state": args.state_disk, "workspace": args.workspace_disk},
            storage_class=args.storage_class,
            deadline=deadline,
        )
        await _within(_patch_vm(custom, args.namespace, name, uid, {"spec": {"runStrategy": "Always"}}), deadline)
        vm, vmi, pod = await _wait_ready(
            custom, core, api_client, args.namespace, name, uid, timeout_s=_remaining(deadline)
        )
        print(
            json.dumps(
                {
                    "action": "created",
                    "vm": _resource_view(vm, kind="VirtualMachine"),
                    "vmi": _resource_view(vmi, kind="VirtualMachineInstance"),
                    "launcher_pod": _pod_view(pod, api_client),
                    "image": args.image,
                    "state_disk": args.state_disk,
                    "workspace_disk": args.workspace_disk,
                    "port_forward": _port_forward(args.namespace, pod),
                },
                sort_keys=True,
            )
        )


async def inspect(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    async with _apis(args.namespace, kubeconfig=args.kubeconfig) as (core, custom, api_client):
        vm = await _within(_vm(custom, args.namespace, args.name), deadline)
        name, namespace, uid = _vm_identity(vm)
        vmi = await _within(_owned_vmi(custom, namespace, name, uid), deadline)
        pod = await _within(_launcher_pod(core, namespace, name, vmi), deadline) if vmi is not None else None
        result: dict[str, Any] = {
            "action": "inspect",
            "vm": _resource_view(vm, kind="VirtualMachine"),
            "vmi": _resource_view(vmi, kind="VirtualMachineInstance") if vmi is not None else None,
            "launcher_pod": _pod_view(pod, api_client) if pod is not None else None,
            "port_forward": _port_forward(namespace, pod) if pod is not None else None,
        }
        if args.target is not None:
            runner = RunnerClient(grpc.aio.insecure_channel(args.target))
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


async def replace_image(args: argparse.Namespace) -> None:
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", args.image):
        raise ValueError("--image must be a digest-pinned image reference")
    deadline = asyncio.get_running_loop().time() + args.timeout
    async with _apis(args.namespace, kubeconfig=args.kubeconfig) as (_core, custom, _api_client):
        vm = await _within(_vm(custom, args.namespace, args.name), deadline)
        if _vm_identity(vm)[2] != args.uid:
            raise ValueError(f"VM {args.name!r} UID does not match --uid")
        if vm.get("spec", {}).get("runStrategy") != "Halted":
            raise RuntimeError("halt the VM and wait for VMI deletion before replacing its root image")
        if await _within(_owned_vmi(custom, args.namespace, args.name, args.uid), deadline) is not None:
            raise RuntimeError("wait for exact VMI deletion before replacing the root image")
        spec = vm.get("spec", {}).get("template", {}).get("spec", {})
        volumes = spec.get("volumes", [])
        roots = [item for item in volumes if item.get("name") == "root"]
        if len(roots) != 1 or not isinstance(roots[0].get("containerDisk"), dict):
            raise ValueError("VM does not have one containerDisk root volume")
        replacement = [
            {**item, "containerDisk": {**item["containerDisk"], "image": args.image}}
            if item.get("name") == "root"
            else item
            for item in volumes
        ]
        await _within(
            _patch_vm(
                custom,
                args.namespace,
                args.name,
                args.uid,
                {"spec": {"template": {"spec": {"volumes": replacement}}}},
                expected_resource_version=_metadata(vm, kind="VM")["resourceVersion"],
            ),
            deadline,
        )
        replaced = await _within(_vm(custom, args.namespace, args.name), deadline)
        print(
            json.dumps(
                {
                    "action": "image_replaced",
                    "vm": _resource_view(replaced, kind="VirtualMachine"),
                    "image": args.image,
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


async def _retire_format_permission(core: k8s_client.CoreV1Api, namespace: str, name: str, vm_uid: str) -> None:
    config_name = _aux_name(name, "config")
    config_map = await core.read_namespaced_config_map(config_name, namespace)
    owners = config_map.metadata.owner_references if config_map.metadata is not None else []
    if (
        len(owners or []) != 1
        or owners[0].api_version != _KUBEVIRT_API_VERSION
        or owners[0].kind != "VirtualMachine"
        or owners[0].name != name
        or owners[0].uid != vm_uid
    ):
        raise RuntimeError(f"ConfigMap {config_name!r} is not owned by VM UID {vm_uid}")
    data = dict(config_map.data or {})
    try:
        guest_config = json.loads(data[_FORMAT_CONFIG_KEY])
    except KeyError, ValueError:
        raise RuntimeError("guest config is missing or invalid") from None
    if guest_config.get("environment_id") != vm_uid:
        raise RuntimeError("guest config VM UID does not match --uid")
    formats = guest_config.get("format_blank_disks")
    if formats == []:
        return
    if formats != ["state", "workspace"]:
        raise RuntimeError("guest config has unexpected blank-disk format permission")
    guest_config["format_blank_disks"] = []
    data[_FORMAT_CONFIG_KEY] = json.dumps(guest_config, separators=(",", ":"))
    await cast(_ConfigMapMergePatchClient, core).patch_namespaced_config_map(
        config_name,
        namespace,
        {
            "metadata": {"uid": config_map.metadata.uid, "resourceVersion": config_map.metadata.resource_version},
            "data": data,
        },
        _content_type="application/merge-patch+json",
    )


async def initialize(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    runner = RunnerClient(grpc.aio.insecure_channel(args.target))
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
    async with _apis(args.namespace, kubeconfig=args.kubeconfig) as (core, custom, _api_client):
        vm = await _within(_vm(custom, args.namespace, args.name), deadline)
        if _vm_identity(vm)[2] != args.uid:
            raise ValueError(f"VM {args.name!r} UID does not match --uid")
        await _within(_retire_format_permission(core, args.namespace, args.name, args.uid), deadline)
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
    runner = RunnerClient(grpc.aio.insecure_channel(args.target), capture_history=True)
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
    runner = RunnerClient(grpc.aio.insecure_channel(args.target), capture_history=True)
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
    async with _apis(args.namespace, kubeconfig=args.kubeconfig) as (core, custom, api_client):
        before = await _within(_vm(custom, args.namespace, args.name), deadline)
        vm_name, namespace, uid = _vm_identity(before)
        old_vmi = await _within(_owned_vmi(custom, namespace, vm_name, uid), deadline)
        if old_vmi is None:
            raise RuntimeError(f"VM {args.name!r} must be running before the stop/start cycle")
        await _within(_patch_vm(custom, namespace, vm_name, uid, {"spec": {"runStrategy": "Halted"}}), deadline)
        await _wait_vmi_absent(custom, namespace, vm_name, uid, deadline=deadline)
        await _resume_with_retry(custom, namespace, vm_name, uid, deadline=deadline)
        after, new_vmi, pod = await _wait_ready(
            custom, core, api_client, namespace, vm_name, uid, timeout_s=_remaining(deadline)
        )
        old_vmi_uid = _metadata(old_vmi, kind="VMI")["uid"]
        new_vmi_uid = _metadata(new_vmi, kind="VMI")["uid"]
        if new_vmi_uid == old_vmi_uid:
            raise RuntimeError("stop/start returned the original VMI UID")
        print(
            json.dumps(
                {
                    "action": "stop_start_completed",
                    "old_vmi_uid": old_vmi_uid,
                    "vm": _resource_view(after, kind="VirtualMachine"),
                    "vmi": _resource_view(new_vmi, kind="VirtualMachineInstance"),
                    "launcher_pod": _pod_view(pod, api_client),
                    "port_forward": _port_forward(namespace, pod),
                },
                sort_keys=True,
            )
        )


async def resume_vm(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    async with _apis(args.namespace, kubeconfig=args.kubeconfig) as (core, custom, api_client):
        before = await _within(_vm(custom, args.namespace, args.name), deadline)
        vm_name, namespace, uid = _vm_identity(before)
        if uid != args.uid:
            raise ValueError(f"VM {args.name!r} UID does not match --uid")
        current_vmi = await _within(_owned_vmi(custom, namespace, vm_name, uid), deadline)
        if current_vmi is not None:
            vmi_uid = _metadata(current_vmi, kind="VMI")["uid"]
            raise RuntimeError(f"VM {args.name!r} still has VMI UID {vmi_uid}")
        print(
            json.dumps({"action": "resuming_vm", "vm": _resource_view(before, kind="VirtualMachine")}, sort_keys=True),
            flush=True,
        )
        await _resume_with_retry(custom, namespace, vm_name, uid, deadline=deadline)
        ready, vmi, pod = await _wait_ready(
            custom, core, api_client, namespace, vm_name, uid, timeout_s=_remaining(deadline)
        )
        print(
            json.dumps(
                {
                    "action": "resumed",
                    "vm": _resource_view(ready, kind="VirtualMachine"),
                    "vmi": _resource_view(vmi, kind="VirtualMachineInstance"),
                    "launcher_pod": _pod_view(pod, api_client),
                    "port_forward": _port_forward(namespace, pod),
                },
                sort_keys=True,
            )
        )


async def recover(args: argparse.Namespace) -> None:
    deadline = asyncio.get_running_loop().time() + args.timeout
    runner = RunnerClient(grpc.aio.insecure_channel(args.target), capture_history=True)
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

    create_parser = commands.add_parser("create", help="create and boot one disposable runner VM")
    create_parser.add_argument("--namespace", required=True)
    create_parser.add_argument("--name", required=True, help="DNS slug; the fixture appends a random suffix")
    create_parser.add_argument("--image", required=True, help="digest-pinned runner containerDisk image")
    create_parser.add_argument("--storage-class", required=True)
    create_parser.add_argument("--image-pull-secret", default=_IMAGE_PULL_SECRET)
    create_parser.add_argument("--llm-base-url", required=True)
    create_parser.add_argument("--proxy-url", required=True)
    create_parser.add_argument("--ca-bundle-config-map", required=True)
    create_parser.add_argument("--kubernetes-host", required=True)
    create_parser.add_argument("--cpu-cores", type=int, default=4)
    create_parser.add_argument("--memory", default="10Gi")
    create_parser.add_argument("--state-disk", default="20Gi")
    create_parser.add_argument("--workspace-disk", default="40Gi")
    create_parser.add_argument("--node-selector", action="append", type=_node_selector, default=[])
    create_parser.add_argument("--model-context-window", action="append", type=_context_window, default=[])
    create_parser.add_argument("--timeout", type=float, default=3600)
    _add_kubeconfig(create_parser)
    create_parser.set_defaults(handler=create)

    inspect_parser = commands.add_parser("inspect", help="show raw VM/VMI/Pod status and optionally ListSessions")
    inspect_parser.add_argument("--namespace", required=True)
    inspect_parser.add_argument("--name", required=True)
    inspect_parser.add_argument("--target", type=_localhost_target)
    inspect_parser.add_argument("--timeout", type=float, default=60)
    _add_kubeconfig(inspect_parser)
    inspect_parser.set_defaults(handler=inspect)

    replace_parser = commands.add_parser(
        "replace-image", help="replace only the root image for one halted experiment VM"
    )
    replace_parser.add_argument("--namespace", required=True)
    replace_parser.add_argument("--name", required=True)
    replace_parser.add_argument("--uid", required=True)
    replace_parser.add_argument("--image", required=True, help="new digest-pinned runner image")
    replace_parser.add_argument("--timeout", type=float, default=300)
    _add_kubeconfig(replace_parser)
    replace_parser.set_defaults(handler=replace_image)

    initialize_parser = commands.add_parser("initialize", help="ListSessions and run deterministic guest probes")
    initialize_parser.add_argument("--namespace", required=True)
    initialize_parser.add_argument("--name", required=True)
    initialize_parser.add_argument("--uid", required=True, help="the VM UID printed by create")
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

    restart_parser = commands.add_parser("stop-start", help="halt, wait for exact VMI deletion, and resume")
    restart_parser.add_argument("--namespace", required=True)
    restart_parser.add_argument("--name", required=True)
    restart_parser.add_argument("--timeout", type=float, default=900)
    _add_kubeconfig(restart_parser)
    restart_parser.set_defaults(handler=stop_start)

    resume_parser = commands.add_parser("resume", help="resume a halted VM after exact VMI deletion")
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
