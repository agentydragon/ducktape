"""The launcher relay policy in the real Kyverno CLI. The CLI loads no API reads, so the test supplies
the VMI, VirtualMachine and ServiceAccount the policy would read; the ownership checks still run."""

from copy import deepcopy
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting  # Testing aliased so pytest does not collect it
from more_itertools import one

from agentplane.crd_group import GROUP, VERSION
from cluster.cdk8s.agentplane.launcher_relay import (
    CONTROLLER_USERNAME,
    LAUNCHER_LABELS,
    RUNNER_CREDENTIAL_ID_ANNOTATION,
    RUNNER_CREDENTIAL_LABEL,
    RUNNER_CREDENTIAL_VMI_UID_ANNOTATION,
    RUNNER_TLS_VOLUME_NAME,
    LauncherRelayPolicy,
)
from cluster.cdk8s.agentplane.sandbox_pod import EGRESS_TOKEN_VOLUME_NAME, SIDECAR_SETTINGS_VOLUME_NAME
from cluster.cdk8s.kyverno.apply import KyvernoApplyResult, apply_policy

NAMESPACE = "launcher-relay-test"
TOKEN_VOLUME = EGRESS_TOKEN_VOLUME_NAME
RELAY_SETTINGS = "relay-test-relay-settings"
RUNNER_CREDENTIAL_ID = "0123456789abcdef0123456789abcdef"
RUNNER_POD = {
    "metadata": {"labels": {"app": "relay-test-runner"}},
    "spec": {
        "imagePullSecrets": [{"name": "relay-test-pull"}],
        "containers": [
            {"name": "runner", "image": "runner:test", "volumeMounts": [{"name": "state", "mountPath": "/state"}]}
        ],
        "initContainers": [
            {
                "name": "egress-sidecar",
                "restartPolicy": "Always",
                "image": "sidecar:test",
                "env": [{"name": "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE", "value": "/etc/relay-test/settings.yaml"}],
                "securityContext": {"allowPrivilegeEscalation": False},
                "volumeMounts": [
                    {"name": TOKEN_VOLUME, "mountPath": "/var/run/relay-test", "readOnly": True},
                    {"name": SIDECAR_SETTINGS_VOLUME_NAME, "mountPath": "/etc/relay-test", "readOnly": True},
                ],
            }
        ],
        "volumes": [
            {"name": "unrelated", "configMap": {"name": "relay-test-config"}},
            {"name": SIDECAR_SETTINGS_VOLUME_NAME, "configMap": {"name": "relay-test-settings"}},
            {
                "name": TOKEN_VOLUME,
                "projected": {"sources": [{"serviceAccountToken": {"audience": "relay-test", "path": "token"}}]},
            },
        ],
    },
}


def owner(api_version: str, kind: str, uid: str) -> dict:
    return {"apiVersion": api_version, "kind": kind, "name": "sandbox-a", "uid": uid, "controller": True}


@pytest.fixture(scope="module")
def policy() -> dict:
    chart = Chart(App(), "test")
    LauncherRelayPolicy(
        chart, "policy", namespace=NAMESPACE, runner_pod=deepcopy(RUNNER_POD), settings_config_map=RELAY_SETTINGS
    )
    rendered: dict = Cdk8sTesting.synth(chart)[0]
    # The CLI's policy validation needs a cluster to check match conditions against and panics
    # without one; they narrow which requests reach the policy, not what it decides.
    del rendered["spec"]["webhookConfiguration"]
    return rendered


@pytest.fixture(scope="module")
def tls_policy() -> dict:
    chart = Chart(App(), "test-tls")
    LauncherRelayPolicy(
        chart,
        "policy",
        namespace=NAMESPACE,
        runner_pod=deepcopy(RUNNER_POD),
        settings_config_map=RELAY_SETTINGS,
        runner_tls_enabled=True,
    )
    rendered: dict = Cdk8sTesting.synth(chart)[0]
    del rendered["spec"]["webhookConfiguration"]
    return rendered


def policy_file(
    tmp_path: Path,
    policy: dict,
    *,
    vm_uid: str,
    vm_owner_kind: str,
    account_owner_uid: str,
    vmi_volumes: list[dict],
    credential_kind: str = "VM",
    sandbox_credential_id: str = RUNNER_CREDENTIAL_ID,
    accepted_credential_id: str = RUNNER_CREDENTIAL_ID,
    last_vmi_uid: str = "vmi-uid",
    vm_credential_id: str = RUNNER_CREDENTIAL_ID,
    vm_vmi_uid: str = "vmi-uid",
    vmi_credential_id: str = RUNNER_CREDENTIAL_ID,
    sandbox_uid: str = "sb-uid",
) -> Path:
    policy = deepcopy(policy)
    reads = {
        "vmi": {
            "metadata": {
                "uid": "vmi-uid",
                "labels": {RUNNER_CREDENTIAL_LABEL: vmi_credential_id},
                "ownerReferences": [owner("kubevirt.io/v1", "VirtualMachine", vm_uid)],
            },
            "spec": {"volumes": vmi_volumes},
        },
        "vm": {
            "metadata": {
                "uid": "vm-uid",
                "annotations": {
                    RUNNER_CREDENTIAL_ID_ANNOTATION: vm_credential_id,
                    RUNNER_CREDENTIAL_VMI_UID_ANNOTATION: vm_vmi_uid,
                },
                "ownerReferences": [owner(f"{GROUP}/{VERSION}", vm_owner_kind, sandbox_uid)],
            }
        },
        "sandboxLive": {
            "metadata": {"uid": sandbox_uid, "name": "sandbox-a"},
            "status": {
                "runnerCredential": {
                    "id": sandbox_credential_id,
                    "kind": credential_kind,
                    "acceptedId": accepted_credential_id,
                    "lastVmiUid": last_vmi_uid,
                }
            },
        },
        "account": {
            "metadata": {
                "name": "sandbox-a",
                "ownerReferences": [owner(f"{GROUP}/{VERSION}", "Sandbox", account_owner_uid)],
            }
        },
    }
    for rule in policy["spec"]["rules"]:
        for entry in rule.get("context", []):
            if "apiCall" in entry:
                del entry["apiCall"]
                entry["variable"] = {"value": reads[entry["name"]]}
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy))
    return path


def launcher() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "virt-launcher-sandbox-a",
            "namespace": NAMESPACE,
            "labels": dict(LAUNCHER_LABELS),
            "ownerReferences": [owner("kubevirt.io/v1", "VirtualMachineInstance", "vmi-uid")],
        },
        "spec": {"containers": [{"name": "compute", "image": "launcher:test"}]},
    }


def apply(
    tmp_path: Path,
    policy: dict,
    resource: dict,
    *,
    username: str = CONTROLLER_USERNAME,
    operation: str = "CREATE",
    vm_uid: str = "vm-uid",
    vm_owner_kind: str = "Sandbox",
    account_owner_uid: str = "sb-uid",
    vmi_volumes: tuple[dict, ...] = (),
    credential_kind: str = "VM",
    sandbox_credential_id: str = RUNNER_CREDENTIAL_ID,
    accepted_credential_id: str = RUNNER_CREDENTIAL_ID,
    last_vmi_uid: str = "vmi-uid",
    vm_credential_id: str = RUNNER_CREDENTIAL_ID,
    vm_vmi_uid: str = "vmi-uid",
    vmi_credential_id: str = RUNNER_CREDENTIAL_ID,
    sandbox_uid: str = "sb-uid",
) -> KyvernoApplyResult:
    path = tmp_path / "pod.yaml"
    path.write_text(yaml.safe_dump(resource))
    return apply_policy(
        policy_file(
            tmp_path,
            policy,
            vm_uid=vm_uid,
            vm_owner_kind=vm_owner_kind,
            account_owner_uid=account_owner_uid,
            vmi_volumes=list(vmi_volumes),
            credential_kind=credential_kind,
            sandbox_credential_id=sandbox_credential_id,
            accepted_credential_id=accepted_credential_id,
            last_vmi_uid=last_vmi_uid,
            vm_credential_id=vm_credential_id,
            vm_vmi_uid=vm_vmi_uid,
            vmi_credential_id=vmi_credential_id,
            sandbox_uid=sandbox_uid,
        ),
        path,
        {"request.userInfo.username": username, "request.operation": operation},
    )


def test_injects_relay_idempotently(tmp_path: Path, policy: dict) -> None:
    first = apply(tmp_path, policy, launcher())
    assert first.ok, first.stdout
    mutated = one(r for r in first.mutated_resources if r["kind"] == "Pod")
    # Kyverno runs again when a later webhook mutates the Pod; the second pass must change nothing.
    second = apply(tmp_path, policy, deepcopy(mutated))
    assert second.ok, second.stdout
    assert second.mutated_resources == first.mutated_resources

    spec = mutated["spec"]
    assert spec["serviceAccountName"] == "sandbox-a"
    assert spec["automountServiceAccountToken"] is False
    assert spec["imagePullSecrets"] == [{"name": "relay-test-pull"}]
    assert [v["name"] for v in spec["volumes"]] == [TOKEN_VOLUME, SIDECAR_SETTINGS_VOLUME_NAME]
    assert not any(c["name"] == "egress-sidecar" for c in spec["containers"])
    relay = one(c for c in spec["initContainers"] if c["name"] == "egress-sidecar")
    assert relay["restartPolicy"] == "Always"
    assert relay["image"] == "sidecar:test"
    assert {mount["name"] for mount in relay["volumeMounts"]} == {TOKEN_VOLUME, SIDECAR_SETTINGS_VOLUME_NAME}
    # The runner's settings listen on loopback; the guest dials the relay from outside it.
    assert one(v for v in spec["volumes"] if v["name"] == SIDECAR_SETTINGS_VOLUME_NAME)["configMap"] == {
        "name": RELAY_SETTINGS
    }
    assert relay["securityContext"]["runAsNonRoot"] is True
    compute = one(c for c in spec["containers"] if c["name"] == "compute")
    assert not compute.get("volumeMounts")

    update = apply(tmp_path, policy, deepcopy(mutated), operation="UPDATE")
    assert update.ok, update.stdout

    legacy = deepcopy(mutated)
    legacy_relay = one(c for c in legacy["spec"]["initContainers"] if c["name"] == "egress-sidecar")
    legacy_relay.pop("restartPolicy")
    legacy["spec"]["initContainers"].remove(legacy_relay)
    legacy["spec"]["containers"].append(legacy_relay)
    legacy_update = apply(tmp_path, policy, legacy, operation="UPDATE")
    assert legacy_update.ok, legacy_update.stdout


def test_mtls_relay_gets_status_bound_secret_and_workload_does_not(tmp_path: Path, tls_policy: dict) -> None:
    first = apply(tmp_path, tls_policy, launcher())
    assert first.ok, first.stdout
    mutated = one(r for r in first.mutated_resources if r["kind"] == "Pod")
    spec = mutated["spec"]
    tls_volume = one(v for v in spec["volumes"] if v["name"] == RUNNER_TLS_VOLUME_NAME)
    assert tls_volume["projected"]["defaultMode"] == 292
    assert tls_volume["projected"]["sources"][0]["secret"]["name"] == f"runner-{RUNNER_CREDENTIAL_ID}"
    assert tls_volume["projected"]["sources"][1]["configMap"] == {
        "name": "cluster-internal-ca-bundle",
        "items": [{"key": "ca-certificates.crt", "path": "ca.crt"}],
    }
    relay = one(c for c in spec["initContainers"] if c["name"] == "egress-sidecar")
    assert {
        "name": RUNNER_TLS_VOLUME_NAME,
        "mountPath": "/run/agentplane/environment-runtime-tls",
        "readOnly": True,
    } in relay["volumeMounts"]
    assert not any(
        mount["name"] == RUNNER_TLS_VOLUME_NAME for c in spec["containers"] for mount in c.get("volumeMounts", [])
    )
    assert not any(
        mount["name"] == RUNNER_TLS_VOLUME_NAME
        for c in spec["initContainers"]
        if c is not relay
        for mount in c.get("volumeMounts", [])
    )

    second = apply(tmp_path, tls_policy, deepcopy(mutated))
    assert second.ok, second.stdout
    assert second.mutated_resources == first.mutated_resources

    workload_mount = deepcopy(mutated)
    workload_mount["spec"]["containers"][0].setdefault("volumeMounts", []).append(
        {"name": RUNNER_TLS_VOLUME_NAME, "mountPath": "/runner-tls", "readOnly": True}
    )
    update = apply(tmp_path, tls_policy, workload_mount, operation="UPDATE")
    assert update.failed > 0

    create_with_workload_mount = launcher()
    create_with_workload_mount["spec"]["containers"][0]["volumeMounts"] = [
        {"name": RUNNER_TLS_VOLUME_NAME, "mountPath": "/runner-tls", "readOnly": True}
    ]
    create = apply(tmp_path, tls_policy, create_with_workload_mount)
    assert create.failed > 0


@pytest.mark.parametrize("with_existing_init", [False, True])
def test_keeps_filesystem_daemons_until_compute_exits(tmp_path: Path, policy: dict, with_existing_init: bool) -> None:
    resource = launcher()
    daemons = [
        {
            "name": f"virtiofs-{volume}",
            "image": "launcher:test",
            "command": ["/usr/libexec/virtiofsd"],
            "args": [f"--socket-path=/sockets/{volume}.sock", f"--shared-dir=/{volume}", "--sandbox=none"],
            "volumeMounts": [{"name": volume, "mountPath": f"/{volume}"}],
            "resources": {"requests": {"memory": "1M"}, "limits": {"memory": "80M"}},
            "securityContext": {"runAsUser": 107, "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]}},
        }
        for volume in ["state", "workspace", "runner-tls", "runner-trust"]
    ]
    unrelated = {"name": "virtiofs-unrelated", "image": "other:test", "command": ["/bin/other"]}
    container_path = {
        "name": "virtiofs-config",
        "image": "launcher:test",
        "command": ["/usr/libexec/virtiofsd"],
        "args": ["--shared-dir=/config"],
    }
    resource["spec"]["containers"][0]["readinessProbe"] = {"exec": {"command": ["/usr/bin/virt-probe"]}}
    original_compute = deepcopy(resource["spec"]["containers"][0])
    existing_init = (
        [
            {"name": name, "image": "launcher:test", "command": ["/bin/copy"]}
            for name in ["container-disk-copy", "prepare-paths"]
        ]
        if with_existing_init
        else []
    )
    resource["spec"]["containers"].extend([*daemons[:2], unrelated, container_path, *daemons[2:]])
    if with_existing_init:
        resource["spec"]["initContainers"] = existing_init
    volumes = (
        {"name": "state", "persistentVolumeClaim": {"claimName": "state"}},
        {"name": "workspace", "persistentVolumeClaim": {"claimName": "workspace"}},
        {"name": "runner-tls", "secret": {"secretName": "runner-tls"}},
        {"name": "runner-trust", "configMap": {"name": "runner-trust"}},
        {"name": "config", "containerPath": {"path": "/config"}},
    )

    first = apply(tmp_path, policy, resource, vmi_volumes=volumes)
    assert first.ok, first.stdout
    mutated = one(first.mutated_resources)
    spec = mutated["spec"]
    assert one(c for c in spec["containers"] if c["name"] == "compute") == original_compute
    assert one(c for c in spec["containers"] if c["name"] == unrelated["name"]) == unrelated
    assert one(c for c in spec["containers"] if c["name"] == container_path["name"]) == container_path
    assert not any(c["name"] in {d["name"] for d in daemons} for c in spec["containers"])
    assert spec["initContainers"][: len(existing_init)] == existing_init
    relay = one(c for c in spec["initContainers"] if c["name"] == "egress-sidecar")
    assert relay["restartPolicy"] == "Always"
    assert {c["name"]: c for c in spec["initContainers"][len(existing_init) :] if c["name"] != relay["name"]} == {
        daemon["name"]: {**daemon, "restartPolicy": "Always"} for daemon in daemons
    }

    second = apply(tmp_path, policy, deepcopy(mutated), vmi_volumes=volumes)
    assert second.ok, second.stdout
    assert second.mutated_resources == first.mutated_resources


@pytest.mark.parametrize(
    "forgery",
    [
        pytest.param({"username": "system:serviceaccount:attacker:creator"}, id="not-kubevirt"),
        pytest.param({"vm_uid": "replaced-vm"}, id="stale-vm"),
        pytest.param({"vm_owner_kind": "NotASandbox"}, id="vm-not-a-sandbox"),
        pytest.param({"account_owner_uid": "other-sandbox"}, id="account-of-another-sandbox"),
        pytest.param(
            {"vmi_volumes": ({"name": "sa", "serviceAccount": {"serviceAccountName": "x"}},)}, id="guest-token"
        ),
    ],
)
def test_rejects_unproven_launcher(tmp_path: Path, policy: dict, forgery: dict) -> None:
    assert apply(tmp_path, policy, launcher(), **forgery).failed > 0


@pytest.mark.parametrize(
    "binding",
    [
        pytest.param({"credential_kind": "Pod"}, id="not-a-vm-binding"),
        pytest.param({"sandbox_credential_id": "fedcba9876543210fedcba9876543210"}, id="status-id-mismatch"),
        pytest.param({"accepted_credential_id": "fedcba9876543210fedcba9876543210"}, id="not-accepted"),
        pytest.param({"last_vmi_uid": "stale-vmi"}, id="status-vmi-mismatch"),
        pytest.param({"vm_vmi_uid": "stale-vmi"}, id="vm-annotation-vmi-mismatch"),
        pytest.param({"vmi_credential_id": "fedcba9876543210fedcba9876543210"}, id="vmi-label-mismatch"),
        pytest.param({"sandbox_uid": "replacement-sandbox"}, id="sandbox-owner-mismatch"),
    ],
)
def test_mtls_rejects_launcher_without_current_sandbox_status_binding(
    tmp_path: Path, tls_policy: dict, binding: dict
) -> None:
    assert apply(tmp_path, tls_policy, launcher(), **binding).failed > 0


def test_rejects_stale_vmi(tmp_path: Path, policy: dict) -> None:
    resource = launcher()
    resource["metadata"]["ownerReferences"][0]["uid"] = "replaced-vmi"
    assert apply(tmp_path, policy, resource).failed > 0


def test_rejects_relay_in_both_container_classes(tmp_path: Path, policy: dict) -> None:
    resource = launcher()
    resource["spec"]["containers"].append(
        {
            "name": "egress-sidecar",
            "image": "sidecar:test",
            "volumeMounts": [{"name": TOKEN_VOLUME, "mountPath": "/var/run/relay-test"}],
        }
    )
    assert apply(tmp_path, policy, resource).failed > 0


@pytest.mark.parametrize(
    ("field", "operation"), [("containers", "CREATE"), ("initContainers", "CREATE"), ("ephemeralContainers", "UPDATE")]
)
def test_rejects_token_mount_outside_relay(tmp_path: Path, policy: dict, field: str, operation: str) -> None:
    resource = launcher()
    resource["spec"].setdefault(field, []).append(
        {"name": "intruder", "image": "probe:test", "volumeMounts": [{"name": TOKEN_VOLUME, "mountPath": "/t"}]}
    )
    assert apply(tmp_path, policy, resource, operation=operation).failed > 0


if __name__ == "__main__":
    pytest_bazel.main()
