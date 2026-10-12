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
from cluster.cdk8s.agentplane.launcher_relay import CONTROLLER_USERNAME, LAUNCHER_LABELS, LauncherRelayPolicy
from cluster.validation.kyverno.apply import KyvernoApplyResult, apply_policy

NAMESPACE = "launcher-relay-test"
TOKEN_VOLUME = "relay-test-token"
RUNNER_POD = {
    "metadata": {"labels": {"app": "relay-test-runner"}},
    "spec": {
        "imagePullSecrets": [{"name": "relay-test-pull"}],
        "containers": [
            {"name": "runner", "image": "runner:test", "volumeMounts": [{"name": "state", "mountPath": "/state"}]},
            {
                "name": "egress-sidecar",
                "image": "sidecar:test",
                "env": [{"name": "AGENTPLANE_EGRESS_SIDECAR_PROXY_HOST", "value": "proxy.test"}],
                "securityContext": {"allowPrivilegeEscalation": False},
                "volumeMounts": [{"name": TOKEN_VOLUME, "mountPath": "/var/run/relay-test", "readOnly": True}],
            },
        ],
        "volumes": [
            {"name": "unrelated", "configMap": {"name": "relay-test-config"}},
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
    LauncherRelayPolicy(chart, "policy", namespace=NAMESPACE, runner_pod=deepcopy(RUNNER_POD))
    rendered: dict = Cdk8sTesting.synth(chart)[0]
    # The CLI's policy validation needs a cluster to check match conditions against and panics
    # without one; they narrow which requests reach the policy, not what it decides.
    del rendered["spec"]["webhookConfiguration"]
    return rendered


def policy_file(
    tmp_path: Path, policy: dict, *, vm_uid: str, vm_owner_kind: str, account_owner_uid: str, vmi_volumes: list[dict]
) -> Path:
    policy = deepcopy(policy)
    reads = {
        "vmi": {
            "metadata": {"uid": "vmi-uid", "ownerReferences": [owner("kubevirt.io/v1", "VirtualMachine", vm_uid)]},
            "spec": {"volumes": vmi_volumes},
        },
        "vm": {
            "metadata": {"uid": "vm-uid", "ownerReferences": [owner(f"{GROUP}/{VERSION}", vm_owner_kind, "sb-uid")]}
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
    assert [v["name"] for v in spec["volumes"]] == [TOKEN_VOLUME]
    relay = one(c for c in spec["containers"] if c["name"] == "egress-sidecar")
    # The guest dials the relay from outside the Pod's loopback.
    assert {"name": "AGENTPLANE_EGRESS_SIDECAR_LISTEN_HOST", "value": "0.0.0.0"} in relay["env"]
    assert relay["securityContext"]["runAsNonRoot"] is True
    compute = one(c for c in spec["containers"] if c["name"] == "compute")
    assert not compute.get("volumeMounts")


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


def test_rejects_stale_vmi(tmp_path: Path, policy: dict) -> None:
    resource = launcher()
    resource["metadata"]["ownerReferences"][0]["uid"] = "replaced-vmi"
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
