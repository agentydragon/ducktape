"""Observe the admitted KubeVirt launcher Pod and verify file-backed virtiofs sidecars."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

VIRTIOFSD = "/usr/libexec/virtiofsd"
FILE_BACKED_VOLUME_SOURCES = ("persistentVolumeClaim", "secret", "configMap")
TOPOLOGY_OBSERVATION_SECONDS = 30.0
TOPOLOGY_POLL_SECONDS = 0.5


class LauncherTopologyObservationError(RuntimeError):
    """The exact ready Pod/VMI identity could not be observed consistently."""


class ClusterReader(Protocol):
    """The namespace-scoped read-only Kubernetes surface used by the VM fixture."""

    @property
    def namespace(self) -> str: ...

    async def kubectl(
        self,
        *args: str,
        body: object | None = None,
        check_failure: Callable[[], None] | None = None,
        timeout_s: float = 310,
    ) -> str: ...


class ProjectedPod(Protocol):
    """The stable pod identity carried by Sandbox readiness diagnostics."""

    @property
    def name(self) -> str: ...

    @property
    def namespace(self) -> str: ...

    @property
    def uid(self) -> str: ...


def is_vm_template(template: str) -> bool:
    """Select all VM templates without probing ordinary Pod Sandbox templates."""
    return template == "environment-vm" or template.startswith("environment-vm-")


def _state_name(container_status: dict[str, Any] | None) -> str:
    if container_status is None:
        return "missing"
    state = container_status.get("state")
    if isinstance(state, dict):
        for name in ("running", "waiting", "terminated"):
            if name in state:
                return name
    return "unknown"


def _container_rows(pod: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    spec = pod.get("spec")
    if not isinstance(spec, dict):
        return []
    rows = spec.get(kind)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _command_first(container: dict[str, Any]) -> str | None:
    command = container.get("command")
    return command[0] if isinstance(command, list) and command and isinstance(command[0], str) else None


def _file_backed_filesystems(vmi: dict[str, Any]) -> list[tuple[str, str]]:
    """Apply the admission policy's PVC/Secret/ConfigMap source classification to VMI filesystems.

    Intersecting with VMI filesystem devices matters: a PVC-backed block disk such as the nested
    ext4 workspace is not a virtiofs daemon. ContainerDisk/ContainerPath sources also remain
    outside this sidecar policy.
    """
    spec = vmi.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    domain = spec.get("domain")
    domain = domain if isinstance(domain, dict) else {}
    devices = domain.get("devices")
    devices = devices if isinstance(devices, dict) else {}
    filesystems = devices.get("filesystems")
    filesystem_names = (
        {
            filesystem["name"]
            for filesystem in filesystems
            if isinstance(filesystem, dict)
            and isinstance(filesystem.get("name"), str)
            and filesystem.get("virtiofs") is not None
        }
        if isinstance(filesystems, list)
        else set()
    )

    volumes = spec.get("volumes")
    result: list[tuple[str, str]] = []
    if isinstance(volumes, list):
        for volume in volumes:
            if not isinstance(volume, dict):
                continue
            name = volume.get("name")
            if not isinstance(name, str) or name not in filesystem_names:
                continue
            source = next((field for field in FILE_BACKED_VOLUME_SOURCES if volume.get(field)), None)
            if source is not None:
                result.append((name, source))
    return sorted(result)


def inspect_launcher_topology(pod: dict[str, Any], vmi: dict[str, Any]) -> dict[str, Any]:
    """Return sanitized placement evidence and stable issue codes for a live Pod/VMI pair."""
    pod_metadata = pod.get("metadata")
    pod_metadata = pod_metadata if isinstance(pod_metadata, dict) else {}
    vmi_metadata = vmi.get("metadata")
    vmi_metadata = vmi_metadata if isinstance(vmi_metadata, dict) else {}
    pod_status = pod.get("status")
    pod_status = pod_status if isinstance(pod_status, dict) else {}
    vmi_status = vmi.get("status")
    vmi_status = vmi_status if isinstance(vmi_status, dict) else {}

    containers = _container_rows(pod, "containers")
    init_containers = _container_rows(pod, "initContainers")
    container_statuses = pod_status.get("containerStatuses")
    init_statuses = pod_status.get("initContainerStatuses")
    container_status_by_name = (
        {row["name"]: row for row in container_statuses if isinstance(row, dict) and isinstance(row.get("name"), str)}
        if isinstance(container_statuses, list)
        else {}
    )
    init_status_by_name = (
        {row["name"]: row for row in init_statuses if isinstance(row, dict) and isinstance(row.get("name"), str)}
        if isinstance(init_statuses, list)
        else {}
    )

    eligible_filesystems = _file_backed_filesystems(vmi)
    observed_daemons = [
        {
            "name": row.get("name"),
            "command": _command_first(row),
            "placement": placement,
            "restart_policy": row.get("restartPolicy"),
            "state": _state_name(statuses.get(row.get("name"))),
            "ready": statuses.get(row.get("name"), {}).get("ready") if row.get("name") in statuses else None,
        }
        for placement, rows, statuses in (
            ("container", containers, container_status_by_name),
            ("initContainer", init_containers, init_status_by_name),
        )
        for row in rows
        if _command_first(row) == VIRTIOFSD
    ]

    filesystem_reports: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    if not eligible_filesystems:
        issues.append(
            {
                "code": "no_file_backed_filesystem",
                "detail": "VMI has no PVC, Secret, or ConfigMap-backed virtiofs filesystem to verify",
            }
        )

    for volume_name, source in eligible_filesystems:
        daemon_name = f"virtiofs-{volume_name}"
        named_regular = [row for row in containers if row.get("name") == daemon_name]
        named_init = [row for row in init_containers if row.get("name") == daemon_name]
        row_report: dict[str, Any] = {
            "volume": volume_name,
            "source": source,
            "expected_daemon": daemon_name,
            "ordinary_container_count": len(named_regular),
            "init_container_count": len(named_init),
        }
        filesystem_reports.append(row_report)

        if len(named_regular) + len(named_init) == 0:
            issues.append(
                {
                    "code": "virtiofs_daemon_missing",
                    "detail": f"{daemon_name} for {source}-backed filesystem {volume_name!r} is absent",
                }
            )
            continue
        if len(named_regular) + len(named_init) != 1:
            issues.append(
                {
                    "code": "virtiofs_daemon_duplicated",
                    "detail": f"{daemon_name} occurs in {len(named_regular)} ordinary and {len(named_init)} init containers",
                }
            )
            continue
        if named_regular:
            issues.append(
                {
                    "code": "virtiofs_daemon_is_ordinary_container",
                    "detail": f"{daemon_name} remains in spec.containers; expected an initContainer with restartPolicy Always",
                }
            )
            continue

        daemon = named_init[0]
        row_report["restart_policy"] = daemon.get("restartPolicy")
        if _command_first(daemon) != VIRTIOFSD:
            issues.append(
                {
                    "code": "virtiofs_daemon_command_mismatch",
                    "detail": f"{daemon_name} is in initContainers but does not run {VIRTIOFSD}",
                }
            )
        if daemon.get("restartPolicy") != "Always":
            issues.append(
                {
                    "code": "virtiofs_daemon_restart_policy_mismatch",
                    "detail": f"{daemon_name} initContainer restartPolicy is {daemon.get('restartPolicy')!r}; expected 'Always'",
                }
            )
        status = init_status_by_name.get(daemon_name)
        row_report["status"] = _state_name(status)
        row_report["ready"] = status.get("ready") if status is not None else None
        if _state_name(status) != "running":
            issues.append(
                {
                    "code": "virtiofs_daemon_not_running",
                    "detail": f"{daemon_name} initContainer status is {_state_name(status)}; expected running at Sandbox readiness",
                }
            )
        if status is None or status.get("ready") is not True:
            issues.append(
                {
                    "code": "virtiofs_daemon_not_ready",
                    "detail": f"{daemon_name} initContainer is not Ready at Sandbox readiness",
                }
            )

    labels = pod_metadata.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    owner_references = pod_metadata.get("ownerReferences")
    owner_references = owner_references if isinstance(owner_references, list) else []
    vmi_owner_uids = [
        owner.get("uid")
        for owner in owner_references
        if isinstance(owner, dict) and owner.get("kind") == "VirtualMachineInstance"
    ]
    vmi_name = vmi_metadata.get("name")
    if vmi_name is not None and labels.get("vmi.kubevirt.io/id") != vmi_name:
        issues.append(
            {
                "code": "launcher_vmi_id_label_mismatch",
                "detail": f"Pod vmi.kubevirt.io/id label {labels.get('vmi.kubevirt.io/id')!r} does not match VMI {vmi_name!r}",
            }
        )
    vmi_uid = vmi_metadata.get("uid")
    if vmi_uid not in vmi_owner_uids:
        issues.append(
            {
                "code": "launcher_vmi_owner_mismatch",
                "detail": f"Pod ownerReferences VMI UID {vmi_owner_uids!r} does not include observed VMI UID {vmi_uid!r}",
            }
        )
    if labels.get("kubevirt.io/created-by") != vmi_uid:
        issues.append(
            {
                "code": "launcher_created_by_uid_mismatch",
                "detail": "Pod kubevirt.io/created-by label does not match the observed VMI UID",
            }
        )
    if pod_metadata.get("namespace") != vmi_metadata.get("namespace"):
        issues.append(
            {
                "code": "launcher_vmi_namespace_mismatch",
                "detail": "launcher Pod and its observed VMI are in different namespaces",
            }
        )

    pod_conditions = pod_status.get("conditions")
    pod_ready = (
        any(
            isinstance(condition, dict) and condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod_conditions
        )
        if isinstance(pod_conditions, list)
        else False
    )
    if labels.get("kubevirt.io") != "virt-launcher":
        issues.append(
            {
                "code": "launcher_type_label_mismatch",
                "detail": f"Pod kubevirt.io label is {labels.get('kubevirt.io')!r}; expected 'virt-launcher'",
            }
        )
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "pod": {
            "name": pod_metadata.get("name"),
            "namespace": pod_metadata.get("namespace"),
            "uid": pod_metadata.get("uid"),
            "ready": pod_ready,
            "labels": {
                key: labels.get(key)
                for key in ("kubevirt.io", "kubevirt.io/domain", "kubevirt.io/created-by", "vmi.kubevirt.io/id")
            },
            "owner_vmi_uids": vmi_owner_uids,
            "containers": observed_daemons,
        },
        "vmi": {"name": vmi_name, "uid": vmi_uid, "phase": vmi_status.get("phase")},
        "eligible_filesystems": filesystem_reports,
        "issues": issues,
    }


def _is_not_found(error: RuntimeError) -> bool:
    message = str(error).lower()
    return "notfound" in message or "not found" in message


def _artifact_path(pod_uid: str) -> Path:
    return undeclared_outputs_dir() / "vm-artifacts" / "launcher-topology" / f"{pod_uid}.json"


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")


async def _get_resource(
    cluster: ClusterReader, report: dict[str, Any], path: Path, *, operation: str, arguments: tuple[str, ...]
) -> dict[str, Any] | None:
    cause: Exception
    try:
        value = json.loads(await cluster.kubectl(*arguments, "-o", "json", timeout_s=10))
        if not isinstance(value, dict):
            raise ValueError(f"{operation} returned JSON that is not an object")
        return cast(dict[str, Any], value)
    except RuntimeError as error:
        if _is_not_found(error):
            return None
        state = "api_error"
        cause = error
    except (TimeoutError, ValueError, TypeError) as error:
        state = "observation_error"
        cause = error
    report["observation"] = {"state": state, "operation": operation, "detail": str(cause)[:500]}
    _write_report(path, report)
    raise LauncherTopologyObservationError(
        f"{operation} failed while checking the admitted launcher; see {path}: {cause}"
    ) from cause


async def assert_admitted_launcher_topology(
    cluster: ClusterReader, sandbox_name: str, sandbox_uid: str, projected_pod: ProjectedPod
) -> None:
    """Wait for the exact ready launcher/VMI pair, then verify admitted virtiofs placement.

    API/not-found and stale-incarnation failures are distinguished from topology violations. A
    bounded poll lets the KubeVirt VMI, Pod status and Sandbox Ready projection converge before
    inspecting the admitted spec. Every result is retained as a sanitized JSON artifact.
    """
    path = _artifact_path(projected_pod.uid or "unknown-pod")
    report: dict[str, Any] = {
        "sandbox": {"name": sandbox_name, "uid": sandbox_uid},
        "projected_pod": {"name": projected_pod.name, "namespace": projected_pod.namespace, "uid": projected_pod.uid},
        "observation": {"state": "waiting"},
    }
    deadline = asyncio.get_running_loop().time() + TOPOLOGY_OBSERVATION_SECONDS
    if not projected_pod.name or not projected_pod.namespace or not projected_pod.uid:
        report["observation"] = {"state": "incomplete_projected_pod_identity"}
        _write_report(path, report)
        raise LauncherTopologyObservationError(
            f"Sandbox {sandbox_name} reached Ready without a complete launcher Pod identity; see {path}"
        )
    if projected_pod.namespace != cluster.namespace:
        report["observation"] = {"state": "projected_pod_namespace_mismatch", "cluster_namespace": cluster.namespace}
        _write_report(path, report)
        raise LauncherTopologyObservationError(
            f"Sandbox {sandbox_name} launcher Pod namespace {projected_pod.namespace!r} does not match "
            f"the fixture namespace {cluster.namespace!r}; see {path}"
        )

    while True:
        pod = await _get_resource(
            cluster, report, path, operation="get launcher Pod", arguments=("get", "pod", projected_pod.name)
        )
        if pod is None:
            report["observation"] = {"state": "pod_not_found"}

        if pod is not None:
            metadata = pod.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            if metadata.get("uid") != projected_pod.uid or metadata.get("namespace") != projected_pod.namespace:
                report["observation"] = {
                    "state": "pod_uid_mismatch",
                    "observed_pod_uid": metadata.get("uid"),
                    "observed_namespace": metadata.get("namespace"),
                }
            else:
                owners = metadata.get("ownerReferences")
                owners = owners if isinstance(owners, list) else []
                vmi_owners = [
                    owner
                    for owner in owners
                    if isinstance(owner, dict) and owner.get("kind") == "VirtualMachineInstance"
                ]
                if len(vmi_owners) != 1 or not isinstance(vmi_owners[0].get("name"), str):
                    report["observation"] = {"state": "invalid_vmi_owner_reference", "owner_count": len(vmi_owners)}
                    _write_report(path, report)
                    raise LauncherTopologyObservationError(
                        f"launcher Pod {projected_pod.name} does not have exactly one VMI owner; see {path}"
                    )
                vmi_owner = vmi_owners[0]
                vmi = await _get_resource(
                    cluster,
                    report,
                    path,
                    operation="get VMI",
                    arguments=("get", "virtualmachineinstance", vmi_owner["name"]),
                )
                if vmi is None:
                    report["observation"] = {"state": "vmi_not_found"}

                if vmi is not None:
                    report = {
                        "sandbox": {"name": sandbox_name, "uid": sandbox_uid},
                        "projected_pod": {
                            "name": projected_pod.name,
                            "namespace": projected_pod.namespace,
                            "uid": projected_pod.uid,
                        },
                        **inspect_launcher_topology(pod, vmi),
                    }
                    vmi_metadata = vmi.get("metadata")
                    vmi_metadata = vmi_metadata if isinstance(vmi_metadata, dict) else {}
                    vmi_status = vmi.get("status")
                    vmi_status = vmi_status if isinstance(vmi_status, dict) else {}
                    pod_labels = metadata.get("labels")
                    pod_labels = pod_labels if isinstance(pod_labels, dict) else {}
                    pod_status = pod.get("status")
                    pod_status = pod_status if isinstance(pod_status, dict) else {}
                    if vmi_metadata.get("uid") != vmi_owner.get("uid"):
                        report["observation"] = {"state": "vmi_uid_mismatch"}
                    elif vmi_metadata.get("namespace") != projected_pod.namespace:
                        report["observation"] = {"state": "vmi_namespace_mismatch"}
                    elif vmi_metadata.get("name") != pod_labels.get("vmi.kubevirt.io/id"):
                        report["observation"] = {"state": "vmi_id_label_mismatch"}
                    elif pod_labels.get("kubevirt.io/created-by") != vmi_metadata.get("uid"):
                        report["observation"] = {"state": "vmi_created_by_uid_mismatch"}
                    elif vmi_status.get("phase") != "Running":
                        report["observation"] = {"state": "vmi_not_running"}
                    elif not report["pod"]["ready"]:
                        report["observation"] = {"state": "pod_not_ready"}
                    else:
                        report["observation"] = {"state": "observed_ready_pair"}
                        _write_report(path, report)
                        issues = report["issues"]
                        if issues:
                            details = "; ".join(issue["detail"] for issue in issues)
                            raise AssertionError(
                                f"admitted launcher topology invalid for Sandbox {sandbox_name} ({sandbox_uid}) "
                                f"and Pod {projected_pod.name} ({projected_pod.uid}): {details}; see {path}"
                            )
                        return

        if asyncio.get_running_loop().time() >= deadline:
            report["observation"]["timed_out_after_seconds"] = TOPOLOGY_OBSERVATION_SECONDS
            _write_report(path, report)
            raise LauncherTopologyObservationError(
                f"could not observe the same ready launcher Pod/VMI incarnation for Sandbox {sandbox_name} "
                f"within {TOPOLOGY_OBSERVATION_SECONDS}s; see {path}"
            )
        await asyncio.sleep(TOPOLOGY_POLL_SECONDS)
