"""Exercise the rendered policy in Kyverno; mock API reads, never ownership checks."""

from copy import deepcopy
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as CdkTesting

from cluster.cdk8s.agentplane.kubevirt_experiment.policy import (
    CONTROLLER_USERNAME,
    MANAGED_LABEL,
    SERVICE_ACCOUNT_ANNOTATION,
    KubeVirtProxyPolicy,
)
from cluster.validation.kyverno.apply import KyvernoApplyResult, apply_policy

NAMESPACE = "agentplane-vm-prototype-test"


def owner(kind: str, uid: str) -> dict:
    return {"apiVersion": "kubevirt.io/v1", "kind": kind, "name": "vm-a", "uid": uid, "controller": True}


def fixture_policy(tmp_path: Path, *, vm_uid: str = "vm-uid", account_uid: str = "vm-uid") -> Path:
    chart = Chart(App(), "test")
    KubeVirtProxyPolicy(
        chart, "policy", namespace=NAMESPACE, image="relay:test", proxy_host="gateway", approved_templates=["prototype"]
    )
    policy = CdkTesting.synth(chart)[0]
    reads = {
        "vmi": {
            "metadata": {"uid": "vmi-uid", "ownerReferences": [owner("VirtualMachine", vm_uid)]},
            "spec": {"volumes": []},
        },
        "vm": {
            "metadata": {
                "uid": "vm-uid",
                "labels": {MANAGED_LABEL: "true"},
                "annotations": {
                    SERVICE_ACCOUNT_ANNOTATION: "vm-a",
                    "agentplane.allegedly.works/vm-template": "prototype",
                },
            }
        },
        "account": {"metadata": {"name": "vm-a", "ownerReferences": [owner("VirtualMachine", account_uid)]}},
    }
    for rule in policy["spec"]["rules"]:
        for entry in rule.get("context", []):
            if "apiCall" in entry:
                del entry["apiCall"]
                entry["variable"] = {"value": reads[entry["name"]]}
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy))
    return path


def pod() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "launcher",
            "namespace": NAMESPACE,
            "labels": {"kubevirt.io": "virt-launcher"},
            "ownerReferences": [owner("VirtualMachineInstance", "vmi-uid")],
        },
        "spec": {"containers": [{"name": "compute", "image": "launcher:test"}], "automountServiceAccountToken": False},
    }


def apply(
    tmp_path: Path,
    resource: dict,
    *,
    username: str = CONTROLLER_USERNAME,
    vm_uid: str = "vm-uid",
    account_uid: str = "vm-uid",
    operation: str = "CREATE",
) -> KyvernoApplyResult:
    path = tmp_path / "pod.yaml"
    path.write_text(yaml.safe_dump(resource))
    return apply_policy(
        fixture_policy(tmp_path, vm_uid=vm_uid, account_uid=account_uid),
        path,
        {"request.userInfo.username": username, "request.operation": operation},
    )


def test_inject_and_reinvoke(tmp_path: Path) -> None:
    first = apply(tmp_path, pod())
    assert first.ok, first.stdout
    assert first.passed >= 3, first.stdout
    mutated = next(r for r in first.mutated_resources if r["kind"] == "Pod")
    second = apply(tmp_path, deepcopy(mutated))
    assert second.ok, second.stdout
    assert second.mutated_resources == first.mutated_resources
    assert mutated["spec"]["serviceAccountName"] == "vm-a"
    relay = next(c for c in mutated["spec"]["containers"] if c["name"] == "egress-sidecar")
    assert len(relay["volumeMounts"]) == 1
    compute = next(c for c in mutated["spec"]["containers"] if c["name"] == "compute")
    assert not compute.get("volumeMounts")


@pytest.mark.parametrize("kwargs", [{"username": "attacker"}, {"vm_uid": "stale"}, {"account_uid": "other-vm"}])
def test_reject_forged_identity(tmp_path: Path, kwargs: dict) -> None:
    result = apply(tmp_path, pod(), **kwargs)
    assert result.failed > 0, result.stdout


@pytest.mark.parametrize(
    ("field", "operation"), [("containers", "CREATE"), ("initContainers", "CREATE"), ("ephemeralContainers", "UPDATE")]
)
def test_reject_token_mount_outside_relay(tmp_path: Path, field: str, operation: str) -> None:
    resource = pod()
    resource["spec"].setdefault(field, []).append(
        {
            "name": "intruder",
            "image": "probe:test",
            "volumeMounts": [{"name": "agentplane-egress-token", "mountPath": "/token"}],
        }
    )
    result = apply(tmp_path, resource, operation=operation)
    assert result.failed > 0, result.stdout


def test_reject_stale_vmi_uid(tmp_path: Path) -> None:
    resource = pod()
    resource["metadata"]["ownerReferences"][0]["uid"] = "stale"
    result = apply(tmp_path, resource)
    assert result.failed > 0, result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
