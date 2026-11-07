"""Manual one-guest KubeVirt/TCG shutdown diagnostic without the Agentplane service stack."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import App, Chart, Testing as Cdk8sTesting
from python.runfiles import Runfiles

from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.vm import guest_logs, host_qemu, infrastructure
from cluster.cdk8s.agentplane import launcher_relay
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

_NAMESPACE_PREFIX = "vm-tcg-shutdown-"
_OWNER_LABEL = "agentplane.io/diagnostic"
_OWNER_NAMESPACE_LABEL = "agentplane.io/diagnostic-namespace"
_VMI_NAME = "ci-guest"
_STATE_PVC_NAME = "ci-guest-state"
_POLICY_SUFFIX = "-virtiofs-shutdown"
_BOOT_EVIDENCE = "AGENTPLANE_BOOT_EVIDENCE_END status=complete"
_VIRSH_URI = "qemu+unix:///session?socket=/var/run/libvirt/virtqemud-sock"
_BOOT_WAIT_SECONDS = 240
_SHUTDOWN_WAIT_SECONDS = 180
_SKIP_VIRTIOFS_POLICY_ENV = "AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY"


def _skip_virtiofs_shutdown_policy() -> bool:
    """Skip only the namespace-scoped virtiofs native-sidecar rule when explicitly requested."""
    match os.environ.get(_SKIP_VIRTIOFS_POLICY_ENV, "0"):
        case "0":
            return False
        case "1":
            return True
        case value:
            raise ValueError(f"{_SKIP_VIRTIOFS_POLICY_ENV} must be '0' or '1', got {value!r}")


def _layout_paths(runfiles: Runfiles, images: dict[str, str]) -> dict[str, Path]:
    root = runfiles.root()
    return {reference: root / path for reference, path in images.items()}


def _create(k3s: baseline.K3s, resource: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(k3s.kubectl("create", "-f", "-", "-o", "json", input_text=json.dumps(resource), stdout_only=True)),
    )


def _owner_value(namespace: str) -> str:
    return namespace.removeprefix(_NAMESPACE_PREFIX)


def _namespace(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name, "labels": {_OWNER_LABEL: _owner_value(name)}},
    }


def _placeholder_config_map(namespace: str, name: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": {_OWNER_LABEL: _owner_value(namespace)}},
        "data": {"placeholder.txt": "unused by this shutdown-only diagnostic\n"},
    }


def _state_claim(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _STATE_PVC_NAME,
            "namespace": namespace,
            "labels": {_OWNER_LABEL: _owner_value(namespace)},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "local-path",
            "resources": {"requests": {"storage": "1Gi"}},
            "volumeMode": "Filesystem",
        },
    }


def _virtiofs_shutdown_policy(namespace: str) -> dict[str, Any]:
    """Install only the real launcher's generated virtiofs native-sidecar mutation, scoped to this test."""
    chart = Chart(App(), "standalone-vmi-tcg")
    runner_pod = {
        "spec": {
            "initContainers": [
                {"name": launcher_relay.sidecar_config.CONTAINER_NAME, "securityContext": {}, "volumeMounts": []}
            ],
            "volumes": [
                {
                    "name": launcher_relay.sandbox_pod.EGRESS_TOKEN_VOLUME_NAME,
                    "projected": {"sources": [{"serviceAccountToken": {"audience": "unused", "path": "token"}}]},
                },
                {
                    "name": launcher_relay.sandbox_pod.SIDECAR_SETTINGS_VOLUME_NAME,
                    "configMap": {"name": "unused-by-virtiofs-only-policy"},
                },
            ],
        }
    }
    launcher_relay.LauncherRelayPolicy(
        chart,
        "launcher-relay",
        namespace=namespace,
        runner_pod=runner_pod,
        settings_config_map="unused-by-virtiofs-only-policy",
    )
    policy = cast(dict[str, Any], Cdk8sTesting.synth(chart)[0])
    policy["metadata"]["name"] = namespace + _POLICY_SUFFIX
    policy["metadata"].setdefault("labels", {}).update(
        {_OWNER_LABEL: _owner_value(namespace), _OWNER_NAMESPACE_LABEL: namespace}
    )
    rule = next(rule for rule in policy["spec"]["rules"] if rule["name"] == "keep-virtiofs-through-guest-shutdown")
    # The direct VMI has no VirtualMachine/Sandbox chain. Keep the production rule's exact
    # VMI lookup and mutation while excluding relay authentication/injection and unrelated rules.
    rule["context"] = [entry for entry in rule["context"] if entry["name"] in {"vmiOwner", "vmi"}]
    return {**policy, "spec": {**policy["spec"], "rules": [rule]}}


def _create_cluster_policy(k3s: baseline.K3s, namespace: str) -> dict[str, Any]:
    resource = _virtiofs_shutdown_policy(namespace)
    try:
        return _create(k3s, resource)
    except Exception:
        # Recover only this random, namespace-scoped policy if the API committed it before
        # kubectl lost its response.
        current = k3s.kubectl(
            "get",
            "clusterpolicy",
            resource["metadata"]["name"],
            "-o",
            "json",
            "--ignore-not-found=true",
            check=False,
            stdout_only=True,
        )
        if not current:
            raise
        policy = json.loads(current)
        metadata = policy["metadata"]
        if (
            metadata.get("labels", {}).get(_OWNER_LABEL) != _owner_value(namespace)
            or metadata.get("labels", {}).get(_OWNER_NAMESPACE_LABEL) != namespace
        ):
            raise RuntimeError(f"refusing create recovery for unowned ClusterPolicy {metadata.get('name')}")
        return cast(dict[str, Any], policy)


def _disk(name: str, serial: str) -> dict[str, Any]:
    return {"name": name, "serial": serial, "disk": {"bus": "virtio"}}


def _vmi(namespace: str, images: dict[str, str]) -> dict[str, Any]:
    boot_image = next(reference for reference in images if reference.endswith("agentplane-vm-guest-boot:unset"))
    root_image = next(reference for reference in images if reference.endswith("agentplane-vm-guest-root:unset"))
    runner_image = next(reference for reference in images if reference.endswith("agentplane-runner-disk:unset"))
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachineInstance",
        "metadata": {
            "name": _VMI_NAME,
            "namespace": namespace,
            "labels": {_OWNER_LABEL: _owner_value(namespace), "kubevirt.io/domain": _VMI_NAME},
        },
        "spec": {
            "terminationGracePeriodSeconds": 30,
            "domain": {
                "cpu": {"cores": 2},
                "resources": {"requests": {"memory": "1536Mi"}},
                "firmware": {
                    "kernelBoot": {
                        "kernelArgs": (
                            "console=ttyS0 net.ifnames=0 agentplane.boot-evidence=1 "
                            "systemd.mask=agentplane-environment-runtime.service"
                        ),
                        "container": {
                            "image": boot_image,
                            "kernelPath": "/boot/vmlinuz",
                            "initrdPath": "/boot/initramfs.cpio",
                            "imagePullPolicy": "IfNotPresent",
                        },
                    }
                },
                "devices": {
                    "autoattachGraphicsDevice": False,
                    "disks": [
                        _disk("rootdisk", "guest-root"),
                        _disk("runner", "agentplane-runner"),
                        _disk("workspace", "workspace"),
                        _disk("config", "agentplane-config"),
                        _disk("launch", "agentplane-launch"),
                        _disk("trust", "agentplane-trust"),
                    ],
                    "filesystems": [{"name": "state", "virtiofs": {}}],
                    "interfaces": [{"name": "default", "masquerade": {}}],
                },
            },
            "networks": [{"name": "default", "pod": {}}],
            "volumes": [
                {"name": "rootdisk", "containerDisk": {"image": root_image, "imagePullPolicy": "IfNotPresent"}},
                {"name": "runner", "containerDisk": {"image": runner_image, "imagePullPolicy": "IfNotPresent"}},
                {"name": "workspace", "emptyDisk": {"capacity": "1Gi"}},
                {"name": "config", "configMap": {"name": "ci-guest-config"}},
                {"name": "launch", "configMap": {"name": "ci-guest-launch"}},
                {"name": "trust", "configMap": {"name": "ci-guest-trust"}},
                {"name": "state", "persistentVolumeClaim": {"claimName": _STATE_PVC_NAME}},
            ],
        },
    }


def _wait_for_pvc(k3s: baseline.K3s, namespace: str) -> dict[str, Any]:
    k3s.kubectl(
        "wait", "--for=jsonpath={.status.phase}=Bound", f"pvc/{_STATE_PVC_NAME}", "-n", namespace, "--timeout=120s"
    )
    return cast(dict[str, Any], json.loads(k3s.kubectl("get", "pvc", _STATE_PVC_NAME, "-n", namespace, "-o", "json")))


def _launcher(k3s: baseline.K3s, namespace: str) -> dict[str, Any]:
    pods = json.loads(
        k3s.kubectl(
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"kubevirt.io=virt-launcher,kubevirt.io/domain={_VMI_NAME}",
            "-o",
            "json",
        )
    )["items"]
    if len(pods) != 1:
        raise RuntimeError(f"expected one launcher pod for the diagnostic VMI, found {len(pods)}")
    return cast(dict[str, Any], pods[0])


def _launcher_lifecycle_sample(k3s: baseline.K3s, namespace: str, pod_name: str, expected_uid: str) -> dict[str, Any]:
    raw = k3s.kubectl(
        "get", "pod", pod_name, "-n", namespace, "--ignore-not-found=true", "-o", "json", timeout=10, stdout_only=True
    )
    if not raw:
        return {"pod_absent": True}
    pod = json.loads(raw)
    metadata = pod.get("metadata", {})
    actual_uid = metadata.get("uid")
    if actual_uid != expected_uid:
        raise RuntimeError(f"launcher Pod UID changed during lifecycle capture: {actual_uid!r} != {expected_uid!r}")
    status = pod.get("status", {})
    container_statuses = {
        field: [
            {
                "name": item.get("name"),
                "ready": item.get("ready"),
                "restartCount": item.get("restartCount"),
                "state": item.get("state", {}),
                "lastState": item.get("lastState", {}),
            }
            for item in status.get(field, [])
        ]
        for field in ("initContainerStatuses", "containerStatuses")
    }
    return {
        "pod_absent": False,
        "pod_uid": actual_uid,
        "deletion_timestamp": metadata.get("deletionTimestamp"),
        "phase": status.get("phase"),
        **container_statuses,
    }


def _start_launcher_lifecycle_capture(
    k3s: baseline.K3s, namespace: str, pod_name: str, pod_uid: str, destination: Path
) -> tuple[threading.Event, threading.Thread]:
    """Poll only this launcher Pod's lifecycle fields while ordinary VMI deletion waits."""
    stop = threading.Event()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("")

    def write_record(record: dict[str, Any]) -> None:
        record["observed_at_utc"] = datetime.now(UTC).isoformat()
        with destination.open("a") as output:
            output.write(json.dumps(record, sort_keys=True) + "\n")

    def collect() -> None:
        deadline = time.monotonic() + _SHUTDOWN_WAIT_SECONDS + 30
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                sample = _launcher_lifecycle_sample(k3s, namespace, pod_name, pod_uid)
                write_record(sample)
                if sample["pod_absent"]:
                    return
            except Exception as error:
                write_record({"capture_error": repr(error)})
                return
            stop.wait(2)

    thread = threading.Thread(target=collect, name="vm-launcher-lifecycle-capture", daemon=True)
    thread.start()
    return stop, thread


def _stop_launcher_lifecycle_capture(stop: threading.Event, thread: threading.Thread) -> None:
    stop.set()
    thread.join(timeout=12)
    if thread.is_alive():
        raise TimeoutError("launcher lifecycle observer did not stop within its bounded kubectl timeout")


def _count_guest_payload(consoles: Path, payload: bytes) -> int:
    return sum(
        1
        for path in consoles.glob("*/*.cri.log")
        for line in path.read_bytes().splitlines(keepends=True)
        if line.endswith(b"\n") and line.rstrip(b"\r\n").split(b" ", 3)[1:] == [b"stdout", b"F", payload]
    )


def _write_shutdown_request_count(output: Path) -> int:
    consoles = output / "vm-artifacts" / "consoles"
    try:
        count = _count_guest_payload(consoles, host_qemu.SHUTDOWN_REQUESTED)
        result: dict[str, Any] = {
            "marker": host_qemu.SHUTDOWN_REQUESTED.decode(),
            "observed_guest_acpi_request_markers": count,
            "meaning": "count of exact guest-console CRI payloads emitted by the ACPI poweroff handler",
            "qmp_system_powerdown_commands_observed": False,
        }
    except Exception as error:
        count = 0
        result = {"capture_error": repr(error), "qmp_system_powerdown_commands_observed": False}
    (output / "guest-shutdown-request-count.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return count


def _wait_for_guest_boot(k3s: baseline.K3s, namespace: str, pod: str) -> str:
    deadline = time.monotonic() + _BOOT_WAIT_SECONDS
    output = ""
    while time.monotonic() < deadline:
        output = k3s.kubectl("logs", pod, "-n", namespace, "-c", "guest-console-log", check=False)
        if _BOOT_EVIDENCE in output:
            return output
        time.sleep(1)
    raise TimeoutError(
        f"guest did not reach completed boot evidence within {_BOOT_WAIT_SECONDS}s; "
        f"last console output:\n{output[-8000:]}"
    )


def _qemu_evidence(k3s: baseline.K3s, namespace: str, pod: str, output: Path) -> tuple[str, str]:
    compute = ["-n", namespace, "exec", pod, "-c", "compute", "--"]
    domain_list = k3s.kubectl(*compute, "virsh", "-c", _VIRSH_URI, "list", "--name").split()
    if len(domain_list) != 1:
        raise RuntimeError(f"expected exactly one libvirt domain in launcher, found {domain_list!r}")
    domain_xml = k3s.kubectl(*compute, "virsh", "-c", _VIRSH_URI, "dumpxml", domain_list[0])
    (output / "domain.xml").write_text(domain_xml)
    domain_type = ET.fromstring(domain_xml).attrib.get("type")
    if domain_type != "qemu":
        raise AssertionError(f"expected a software-emulated libvirt domain type=qemu, found {domain_type!r}")

    qmp_kvm = k3s.kubectl(
        *compute, "virsh", "-c", _VIRSH_URI, "qemu-monitor-command", domain_list[0], '{"execute":"query-kvm"}'
    )
    (output / "qmp-query-kvm.json").write_text(qmp_kvm + "\n")
    kvm_state = json.loads(qmp_kvm).get("return", {})
    if kvm_state.get("enabled") is not False:
        raise AssertionError(f"expected QEMU KVM acceleration to be disabled, query-kvm returned {qmp_kvm!r}")

    script = r"""for path in /proc/[0-9]*/cmdline; do
  comm_path=${path%cmdline}comm
  [ -r "$path" ] || continue
  IFS= read -r comm < "$comm_path" || continue
  case "$comm" in
    qemu*) printf 'COMM=%s CMDLINE=' "$comm"; tr '\000' ' ' < "$path"; printf '\n' ;;
  esac
done"""
    command_lines = k3s.kubectl(*compute, "sh", "-c", script)
    (output / "qemu-cmdline.txt").write_text(command_lines)
    arguments = [
        line.removeprefix("COMM=").split(" CMDLINE=", 1)[1]
        for line in command_lines.splitlines()
        if " CMDLINE=" in line
    ]
    if not arguments:
        raise RuntimeError(f"could not read a QEMU process command line from launcher {pod}")
    return domain_xml, command_lines


def _wait_for_delete(k3s: baseline.K3s, resource: str, name: str, namespace: str, timeout: int) -> bool:
    k3s.kubectl("wait", "--for=delete", f"{resource}/{name}", "-n", namespace, f"--timeout={timeout}s", check=False)
    return not k3s.kubectl(
        "get", resource, name, "-n", namespace, "--ignore-not-found=true", "-o", "name", check=False, stdout_only=True
    ).strip()


def _create_namespace(k3s: baseline.K3s, name: str) -> dict[str, Any]:
    try:
        return _create(k3s, _namespace(name))
    except Exception:
        # The API may have committed the create before the client lost its response. Recover its
        # UID only when the one-use label proves this randomly named namespace belongs to us.
        current = k3s.kubectl(
            "get", "namespace", name, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
        )
        if current:
            namespace = json.loads(current)
            if namespace["metadata"].get("labels", {}).get(_OWNER_LABEL) == _owner_value(name):
                return cast(dict[str, Any], namespace)
        raise


def _create_namespaced(
    k3s: baseline.K3s, namespace: str, namespace_uid: str, resource: dict[str, Any]
) -> dict[str, Any]:
    """Recover a committed create if kubectl lost its response, only within our owned namespace."""
    try:
        return _create(k3s, resource)
    except Exception:
        current_namespace = k3s.kubectl(
            "get", "namespace", namespace, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
        )
        if not current_namespace:
            raise
        namespace_object = json.loads(current_namespace)
        namespace_metadata = namespace_object["metadata"]
        if namespace_metadata.get("uid") != namespace_uid or namespace_metadata.get("labels", {}).get(
            _OWNER_LABEL
        ) != _owner_value(namespace):
            raise RuntimeError(f"refusing create recovery in namespace {namespace}: its owner identity changed")

        kind = resource["kind"]
        resource_type = {"ConfigMap": "configmap", "PersistentVolumeClaim": "pvc", "VirtualMachineInstance": "vmi"}[
            kind
        ]
        name = resource["metadata"]["name"]
        current_resource = k3s.kubectl(
            "get",
            resource_type,
            name,
            "-n",
            namespace,
            "-o",
            "json",
            "--ignore-not-found=true",
            check=False,
            stdout_only=True,
        )
        if not current_resource:
            raise
        resource_object = json.loads(current_resource)
        metadata = resource_object["metadata"]
        if metadata.get("namespace") != namespace or metadata.get("labels", {}).get(_OWNER_LABEL) != _owner_value(
            namespace
        ):
            raise RuntimeError(f"refusing create recovery for unowned {kind} {name} in {namespace}")
        return cast(dict[str, Any], resource_object)


def _cleanup_namespace(
    k3s: baseline.K3s, name: str, uid: str, pvc_uid: str | None, vmi_uid: str | None, policy_uid: str | None
) -> None:
    namespace_json = k3s.kubectl(
        "get", "namespace", name, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
    )
    if not namespace_json:
        return
    namespace = json.loads(namespace_json)
    if namespace["metadata"].get("uid") != uid or namespace["metadata"].get("labels", {}).get(
        _OWNER_LABEL
    ) != _owner_value(name):
        raise RuntimeError(f"refusing to delete namespace {name}: its UID or ownership label changed")

    vmi_json = k3s.kubectl(
        "get", "vmi", _VMI_NAME, "-n", name, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
    )
    if vmi_json:
        vmi = json.loads(vmi_json)
        if vmi["metadata"].get("uid") != vmi_uid or vmi["metadata"].get("labels", {}).get(_OWNER_LABEL) != _owner_value(
            name
        ):
            raise RuntimeError(f"refusing to delete unowned VMI {_VMI_NAME} in {name}")
        k3s.kubectl("delete", "vmi", _VMI_NAME, "-n", name, "--wait=false", check=False)
        if not _wait_for_delete(k3s, "vmi", _VMI_NAME, name, _SHUTDOWN_WAIT_SECONDS):
            # Failure cleanup is scoped to this action-owned k3s cluster and namespace. The actual
            # experiment above always uses ordinary graceful VMI deletion first.
            k3s.kubectl(
                "delete", "vmi", _VMI_NAME, "-n", name, "--grace-period=0", "--force", "--wait=false", check=False
            )
            if not _wait_for_delete(k3s, "vmi", _VMI_NAME, name, 30):
                raise RuntimeError(f"action-owned VMI {_VMI_NAME} did not disappear during cleanup")

    policy_name = name + _POLICY_SUFFIX
    policy_json = k3s.kubectl(
        "get", "clusterpolicy", policy_name, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
    )
    if policy_json:
        policy = json.loads(policy_json)
        metadata = policy["metadata"]
        if (
            metadata.get("uid") != policy_uid
            or metadata.get("labels", {}).get(_OWNER_LABEL) != _owner_value(name)
            or metadata.get("labels", {}).get(_OWNER_NAMESPACE_LABEL) != name
        ):
            raise RuntimeError(f"refusing to delete ClusterPolicy {policy_name}: its UID or ownership labels changed")
        k3s.kubectl("delete", "clusterpolicy", policy_name, "--wait=true", "--timeout=30s")
        if k3s.kubectl(
            "get", "clusterpolicy", policy_name, "--ignore-not-found=true", "-o", "name", check=False, stdout_only=True
        ).strip():
            raise RuntimeError(f"action-owned ClusterPolicy {policy_name} did not disappear")

    pvc_json = k3s.kubectl(
        "get",
        "pvc",
        _STATE_PVC_NAME,
        "-n",
        name,
        "-o",
        "json",
        "--ignore-not-found=true",
        check=False,
        stdout_only=True,
    )
    if pvc_json:
        pvc = json.loads(pvc_json)
        if pvc["metadata"].get("uid") != pvc_uid or pvc["metadata"].get("labels", {}).get(_OWNER_LABEL) != _owner_value(
            name
        ):
            raise RuntimeError(f"refusing to delete state PVC {_STATE_PVC_NAME}: its UID or ownership label changed")
        k3s.kubectl("delete", "pvc", _STATE_PVC_NAME, "-n", name, "--wait=true", "--timeout=60s", check=False)
        if k3s.kubectl(
            "get",
            "pvc",
            _STATE_PVC_NAME,
            "-n",
            name,
            "--ignore-not-found=true",
            "-o",
            "name",
            check=False,
            stdout_only=True,
        ).strip():
            raise RuntimeError(f"action-owned state PVC {_STATE_PVC_NAME} did not disappear")
    k3s.kubectl("delete", "namespace", name, "--wait=true", "--timeout=90s")
    if k3s.kubectl(
        "get", "namespace", name, "--ignore-not-found=true", "-o", "name", check=False, stdout_only=True
    ).strip():
        raise RuntimeError(f"action-owned namespace {name} survived deletion")
    if pvc_uid:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            volumes = json.loads(
                k3s.kubectl("get", "pv", "-o", "json", check=False, stdout_only=True) or '{"items":[]}'
            )["items"]
            if not any(volume.get("spec", {}).get("claimRef", {}).get("uid") == pvc_uid for volume in volumes):
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"local-path PV for state PVC UID {pvc_uid} survived cleanup")


def _write_failure_report(k3s: baseline.K3s, namespace: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for filename, args in [
        ("cluster-report.txt", ("get", "pods,events", "-A", "-o", "wide")),
        ("namespace.yaml", ("get", "namespace", namespace, "-o", "yaml")),
        ("vmi.yaml", ("get", "vmi", _VMI_NAME, "-n", namespace, "-o", "yaml")),
        ("namespace-pods.yaml", ("get", "pods", "-n", namespace, "-o", "yaml")),
        ("namespace-events.yaml", ("get", "events", "-n", namespace, "--sort-by=.lastTimestamp", "-o", "yaml")),
        ("namespace-pvc.yaml", ("get", "pvc", "-n", namespace, "-o", "yaml")),
        ("namespace-configmaps.yaml", ("get", "configmap", "-n", namespace, "-o", "yaml")),
        ("namespace-launcher-policy.yaml", ("get", "clusterpolicy", namespace + _POLICY_SUFFIX, "-o", "yaml")),
        ("kubevirt.yaml", ("get", "kubevirt", "kubevirt", "-n", infrastructure.KUBEVIRT_NAMESPACE, "-o", "yaml")),
        ("node.yaml", ("get", "node", "env", "-o", "yaml")),
    ]:
        try:
            (output / filename).write_text(k3s.kubectl(*args, check=False))
        except Exception as error:
            (output / filename).write_text(f"report capture failed: {error!r}\n")
    try:
        (output / "k3s-report.txt").write_text(k3s.report(namespace))
    except Exception as error:
        (output / "k3s-report.txt").write_text(f"report capture failed: {error!r}\n")


def test_virtiofs_shutdown_policy_is_the_production_rule_scoped_to_one_namespace() -> None:
    namespace = _NAMESPACE_PREFIX + "unit-test"

    policy = _virtiofs_shutdown_policy(namespace)

    assert policy["metadata"]["name"] == namespace + _POLICY_SUFFIX
    assert policy["metadata"]["labels"][_OWNER_NAMESPACE_LABEL] == namespace
    rules = policy["spec"]["rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["name"] == "keep-virtiofs-through-guest-shutdown"
    resources = rule["match"]["any"][0]["resources"]
    assert resources["namespaces"] == [namespace]
    assert resources["selector"]["matchLabels"] == launcher_relay.LAUNCHER_LABELS
    assert [entry["name"] for entry in rule["context"]] == ["vmiOwner", "vmi"]
    foreach = rule["mutate"]["foreach"]
    assert len(foreach) == 1
    assert foreach[0]["list"] == "request.object.spec.containers"
    patches = foreach[0]["patchesJson6902"]
    assert "/spec/initContainers/-" in patches
    assert '"restartPolicy":"Always"' in patches
    assert "/spec/containers/{{elementIndex}}" in patches


def test_shutdown_policy_cleanup_after_generation_failure_ignores_missing_cluster_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _NAMESPACE_PREFIX + "unit-test"
    namespace_object = {
        "metadata": {"name": namespace, "uid": "owned-uid", "labels": {_OWNER_LABEL: _owner_value(namespace)}}
    }

    class FakeK3s:
        def __init__(self) -> None:
            self.namespace_exists = True
            self.calls: list[tuple[tuple[str, ...], bool]] = []

        def kubectl(self, *args: str, check: bool = True, stdout_only: bool = False) -> str:
            self.calls.append((args, stdout_only))
            if args[:2] == ("get", "namespace"):
                return json.dumps(namespace_object) if self.namespace_exists else ""
            if args[0] == "get" and args[1] in {"vmi", "clusterpolicy", "pvc"}:
                # kubectl can print a NotFound diagnostic on stderr even with --ignore-not-found;
                # stdout-only reads keep that diagnostic from being mistaken for JSON/object data.
                return "" if stdout_only else "Error from server (NotFound): resource not found"
            if args[:2] == ("delete", "namespace"):
                self.namespace_exists = False
                return ""
            raise AssertionError(f"unexpected kubectl call: {args}")

    k3s = FakeK3s()

    def fail_generation(_namespace: str) -> dict[str, Any]:
        raise RuntimeError("policy generation failed before API create")

    monkeypatch.setattr(sys.modules[__name__], "_virtiofs_shutdown_policy", fail_generation)
    with pytest.raises(RuntimeError, match="policy generation failed"):
        _create_cluster_policy(cast(baseline.K3s, k3s), namespace)

    _cleanup_namespace(cast(baseline.K3s, k3s), namespace, "owned-uid", None, None, None)

    assert any(args[:2] == ("delete", "namespace") for args, _ in k3s.calls)
    assert all(args[:2] != ("create", "-") for args, _ in k3s.calls)
    assert any(args[:2] == ("get", "clusterpolicy") and stdout_only for args, stdout_only in k3s.calls)


def test_virtiofs_policy_skip_is_opt_in_and_rejects_other_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SKIP_VIRTIOFS_POLICY_ENV, raising=False)
    assert _skip_virtiofs_shutdown_policy() is False

    monkeypatch.setenv(_SKIP_VIRTIOFS_POLICY_ENV, "1")
    assert _skip_virtiofs_shutdown_policy() is True

    monkeypatch.setenv(_SKIP_VIRTIOFS_POLICY_ENV, "0")
    assert _skip_virtiofs_shutdown_policy() is False

    monkeypatch.setenv(_SKIP_VIRTIOFS_POLICY_ENV, "true")
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        _skip_virtiofs_shutdown_policy()


def test_shutdown_policy_lifecycle_sample_distinguishes_absent_from_api_error_and_replaced_pod() -> None:
    namespace = _NAMESPACE_PREFIX + "unit-test"

    class FakeK3s:
        def __init__(self, result: str = "", error: Exception | None = None) -> None:
            self.result = result
            self.error = error

        def kubectl(self, *_args: str, **kwargs: Any) -> str:
            assert kwargs["timeout"] == 10
            assert kwargs["stdout_only"] is True
            if self.error is not None:
                raise self.error
            return self.result

    assert _launcher_lifecycle_sample(cast(baseline.K3s, FakeK3s()), namespace, "launcher", "pod-uid") == {
        "pod_absent": True
    }

    with pytest.raises(RuntimeError, match="API unavailable"):
        _launcher_lifecycle_sample(
            cast(baseline.K3s, FakeK3s(error=RuntimeError("API unavailable"))), namespace, "launcher", "pod-uid"
        )

    replacement = json.dumps({"metadata": {"uid": "new-pod-uid"}, "status": {}})
    with pytest.raises(RuntimeError, match="Pod UID changed"):
        _launcher_lifecycle_sample(cast(baseline.K3s, FakeK3s(result=replacement)), namespace, "launcher", "pod-uid")

    finished_at = "2026-11-06T23:58:00Z"
    stopped = json.dumps(
        {
            "metadata": {"uid": "pod-uid", "deletionTimestamp": "2026-11-06T23:57:59Z"},
            "status": {
                "phase": "Running",
                "initContainerStatuses": [
                    {
                        "name": "virtiofs-state",
                        "ready": False,
                        "restartCount": 0,
                        "state": {"terminated": {"finishedAt": finished_at}},
                    }
                ],
            },
        }
    )
    sample = _launcher_lifecycle_sample(cast(baseline.K3s, FakeK3s(result=stopped)), namespace, "launcher", "pod-uid")
    assert sample["pod_uid"] == "pod-uid"
    assert sample["initContainerStatuses"][0]["state"]["terminated"]["finishedAt"] == finished_at


@pytest.mark.timeout(1200)
def test_ci_guest_shutdown_via_standalone_kubevirt_vmi() -> None:
    """Boot the CI guest under TCG and request shutdown through normal VMI deletion."""
    output = undeclared_outputs_dir() / "standalone-vmi-tcg"
    output.mkdir(parents=True, exist_ok=True)
    runfiles = Runfiles.CreateOrRaise()
    runfiles_root = runfiles.root()
    binary = runfiles_root / os.environ["K3S_BINARY_RLOCATION"]
    image_manifest = json.loads((runfiles_root / os.environ["K3S_IMAGE_MANIFEST_RLOCATION"]).read_text())
    vm_images = _layout_paths(runfiles, image_manifest["vm"])
    guest_images = _layout_paths(runfiles, image_manifest["guest"])
    all_images = vm_images | guest_images
    (output / "input-images.json").write_text(json.dumps(image_manifest, indent=2, sort_keys=True))

    namespace_name = _NAMESPACE_PREFIX + uuid.uuid4().hex[:10]
    skip_virtiofs_policy = _skip_virtiofs_shutdown_policy()
    work = Path(tempfile.mkdtemp(prefix="k3s-vmi-tcg-", dir=os.environ["TEST_TMPDIR"]))
    k3s = baseline.K3s(work, binary)
    process: subprocess.Popen[bytes] | None = None
    namespace_uid: str | None = None
    vmi_uid: str | None = None
    pvc_uid: str | None = None
    policy_uid: str | None = None
    primary_error: BaseException | None = None

    try:
        baseline.stop_previous()
        process = k3s.start(layers_in_memory=False)
        k3s.import_images(all_images)
        releases = infrastructure.releases()
        infrastructure.install(k3s.kubectl, releases)
        infrastructure.wait_for_apis(k3s.kubectl)
        # Install the namespace-scoped equivalent of the production virtiofs native-sidecar rule
        # below, after creating its random namespace and before any VMI launcher admission.
        infrastructure.wait_until_ready(k3s.kubectl, require_cluster_policies=False)

        namespace = _create_namespace(k3s, namespace_name)
        namespace_uid = namespace["metadata"]["uid"]
        policy: dict[str, Any] | None = None
        if not skip_virtiofs_policy:
            policy = _create_cluster_policy(k3s, namespace_name)
            policy_uid = policy["metadata"]["uid"]
            (output / "virtiofs-shutdown-policy.yaml").write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
            k3s.kubectl(
                "wait",
                "--for=condition=Ready",
                f"clusterpolicy/{namespace_name + _POLICY_SUFFIX}",
                f"--timeout={infrastructure.READY_SECONDS}s",
            )
        (output / "virtiofs-policy-mode.json").write_text(
            json.dumps(
                {
                    "skip_virtiofs_shutdown_policy": skip_virtiofs_policy,
                    "policy_enabled": not skip_virtiofs_policy,
                    "policy_name": policy["metadata"]["name"] if policy else None,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        for name in ("ci-guest-config", "ci-guest-launch", "ci-guest-trust"):
            _create_namespaced(k3s, namespace_name, namespace_uid, _placeholder_config_map(namespace_name, name))
        pvc = _create_namespaced(k3s, namespace_name, namespace_uid, _state_claim(namespace_name))
        pvc_uid = pvc["metadata"]["uid"]
        vmi = _vmi(namespace_name, image_manifest["guest"])
        (output / "vmi.json").write_text(json.dumps(vmi, indent=2, sort_keys=True))

        vmi_was_created = False
        with (
            guest_logs.capture(
                log_root=Path("/var/log/pods"),
                pod_root=Path("/var/lib/kubelet/pods"),
                storage_root=k3s.data_dir / "storage",
                namespace=namespace_name,
                kubevirt_namespace=infrastructure.KUBEVIRT_NAMESPACE,
                destination=output / "vm-artifacts",
                validate_shutdown=lambda: vmi_was_created,
                require_boot_evidence=True,
            ),
            host_qemu.capture(k3s.kubectl, namespace_name, output / "vm-artifacts"),
        ):
            created_vmi = _create_namespaced(k3s, namespace_name, namespace_uid, vmi)
            vmi_was_created = True
            vmi_uid = created_vmi["metadata"]["uid"]
            pvc = _wait_for_pvc(k3s, namespace_name)
            k3s.kubectl(
                "wait",
                "--for=condition=Ready",
                f"vmi/{_VMI_NAME}",
                "-n",
                namespace_name,
                f"--timeout={_BOOT_WAIT_SECONDS}s",
            )
            pod = _launcher(k3s, namespace_name)
            pod_name = pod["metadata"]["name"]
            pod_uid = pod["metadata"]["uid"]
            (output / "launcher-pod.json").write_text(json.dumps(pod, indent=2, sort_keys=True))
            init_containers = pod.get("spec", {}).get("initContainers", [])
            containers = pod.get("spec", {}).get("containers", [])
            state_daemon = [container for container in init_containers if container.get("name") == "virtiofs-state"]
            ordinary_state_daemon = [container for container in containers if container.get("name") == "virtiofs-state"]
            if skip_virtiofs_policy:
                if state_daemon or len(ordinary_state_daemon) != 1:
                    raise AssertionError(
                        "without the scoped virtiofs rule, virtiofs-state must remain one ordinary launcher container"
                    )
            else:
                if len(state_daemon) != 1 or state_daemon[0].get("restartPolicy") != "Always":
                    raise AssertionError(
                        "the namespace-scoped production virtiofs rule did not move virtiofs-state to a restartable init container"
                    )
                if ordinary_state_daemon:
                    raise AssertionError("virtiofs-state remained an ordinary launcher container after admission")
            (output / "virtiofs-native-sidecar-admission.json").write_text(
                json.dumps(
                    {
                        "policy_enabled": not skip_virtiofs_policy,
                        "policy_name": policy["metadata"]["name"] if policy else None,
                        "policy_uid": policy_uid,
                        "policy_namespace_match": namespace_name,
                        "launcher_pod_uid": pod_uid,
                        "virtiofs_state_in_init_containers": bool(state_daemon),
                        "restart_policy": state_daemon[0].get("restartPolicy") if state_daemon else None,
                        "virtiofs_state_in_containers": bool(ordinary_state_daemon),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            (output / "node.yaml").write_text(k3s.kubectl("get", "node", "env", "-o", "yaml"))
            (output / "vmi-running.yaml").write_text(
                k3s.kubectl("get", "vmi", _VMI_NAME, "-n", namespace_name, "-o", "yaml")
            )
            (output / "guest-boot-console.txt").write_text(_wait_for_guest_boot(k3s, namespace_name, pod_name))
            _, qemu_cmdline = _qemu_evidence(k3s, namespace_name, pod_name, output)
            (output / "qemu-pre-shutdown.txt").write_text(
                f"namespace_uid={namespace_uid}\nvmi_uid={vmi_uid}\npvc_uid={pvc['metadata']['uid']}\n"
                f"launcher_pod_uid={pod_uid}\nnode={pod['spec'].get('nodeName')}\n"
                f"/dev/kvm_present_in_compute={k3s.kubectl('-n', namespace_name, 'exec', pod_name, '-c', 'compute', '--', 'sh', '-c', 'test -e /dev/kvm && echo yes || echo no').strip()}\n"
                f"libvirt_domain_type=qemu\n{qemu_cmdline}"
            )

            # A normal VMI deletion asks KubeVirt to send the guest ACPI power button. No
            # QMP command or force-delete participates in the measured shutdown path. The
            # additional read-only observer samples only this Pod's status every 2 seconds.
            lifecycle = output / "launcher-lifecycle.jsonl"
            stop_lifecycle, lifecycle_thread = _start_launcher_lifecycle_capture(
                k3s, namespace_name, pod_name, pod_uid, lifecycle
            )
            try:
                request_started = datetime.now(UTC).isoformat()
                with lifecycle.open("a") as record_file:
                    record_file.write(
                        json.dumps(
                            {"event": "vmi_delete_request_started", "observed_at_utc": request_started}, sort_keys=True
                        )
                        + "\n"
                    )
                k3s.kubectl("delete", "vmi", _VMI_NAME, "-n", namespace_name, "--wait=false")
                with lifecycle.open("a") as record_file:
                    record_file.write(
                        json.dumps(
                            {
                                "event": "vmi_delete_request_returned",
                                "request_started_at_utc": request_started,
                                "observed_at_utc": datetime.now(UTC).isoformat(),
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                if not _wait_for_delete(k3s, "vmi", _VMI_NAME, namespace_name, _SHUTDOWN_WAIT_SECONDS):
                    raise RuntimeError("KubeVirt did not remove the VMI after the bounded graceful deletion")
            finally:
                _stop_launcher_lifecycle_capture(stop_lifecycle, lifecycle_thread)

        request_count = _write_shutdown_request_count(output)
        if request_count < 1:
            raise RuntimeError("ordinary VMI deletion completed without any retained guest ACPI request marker")

    except BaseException as error:
        primary_error = error
        _write_shutdown_request_count(output)
        if process is not None:
            _write_failure_report(k3s, namespace_name, output)
        raise
    finally:
        cleanup_errors: list[Exception] = []
        if process is not None and process.poll() is None and namespace_uid is not None:
            try:
                current_vmi = k3s.kubectl(
                    "get", "vmi", _VMI_NAME, "-n", namespace_name, "-o", "json", "--ignore-not-found=true", check=False
                )
                if current_vmi:
                    obj = json.loads(current_vmi)
                    if obj["metadata"].get("uid") != vmi_uid or obj["metadata"].get("labels", {}).get(
                        _OWNER_LABEL
                    ) != _owner_value(namespace_name):
                        raise RuntimeError("refusing cleanup: the VMI identity or ownership label changed")
                _cleanup_namespace(k3s, namespace_name, namespace_uid, pvc_uid, vmi_uid, policy_uid)
            except Exception as error:
                cleanup_errors.append(error)
                _write_failure_report(k3s, namespace_name, output)
        if process is not None:
            try:
                k3s.stop(process)
            except Exception as error:
                cleanup_errors.append(error)
                _write_failure_report(k3s, namespace_name, output)
        shutil.rmtree(work, ignore_errors=True)
        if cleanup_errors:
            message = "action-owned k3s cleanup failed: " + "; ".join(repr(error) for error in cleanup_errors)
            if primary_error is not None:
                primary_error.add_note(message)
            else:
                raise RuntimeError(message)


if __name__ == "__main__":
    pytest_bazel.main()
