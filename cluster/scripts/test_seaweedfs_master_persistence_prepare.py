"""Identity and ownership guards for the controller adoption manifest."""

import copy
from typing import Any

import pytest
import pytest_bazel

from cluster.scripts.seaweedfs_master_persistence import prepare_member, prepare_statefulset


@pytest.fixture
def resources() -> tuple[dict[str, Any], dict[str, Any]]:
    seaweed = {
        "kind": "Seaweed",
        "metadata": {"name": "store", "namespace": "storage", "uid": "owning-cr"},
        "spec": {
            "master": {
                "persistence": {
                    "enabled": True,
                    "mountPath": "/data",
                    "storageClassName": "scratch-class",
                    "resources": {"requests": {"storage": "2Gi"}},
                }
            }
        },
    }
    sts = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": "store-master",
            "namespace": "storage",
            "uid": "old-controller",
            "resourceVersion": "42",
            "ownerReferences": [{"uid": "owning-cr", "controller": True}],
        },
        "spec": {
            "replicas": 3,
            "serviceName": "store-master-peer",
            "selector": {"matchLabels": {"component": "master"}},
            "template": {
                "metadata": {"labels": {"component": "master"}},
                "spec": {
                    "containers": [
                        {
                            "name": "master",
                            "image": "unchanged-image",
                            "command": ["/bin/sh", "-ec", "weed master -ip=$(POD_NAME).peers -peers=unchanged"],
                        }
                    ]
                },
            },
        },
        "status": {"readyReplicas": 3},
    }
    return sts, seaweed


def test_preparation_preserves_member_identity_without_mutating_input(
    resources: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    sts, seaweed = resources
    before = copy.deepcopy(resources)
    prepared = prepare_statefulset(sts, seaweed)
    assert resources == before
    assert prepared["metadata"]["ownerReferences"] == sts["metadata"]["ownerReferences"]
    assert set(prepared["metadata"]) == {"name", "namespace", "ownerReferences"}
    assert "status" not in prepared
    for field in ("replicas", "serviceName", "selector"):
        assert prepared["spec"][field] == sts["spec"][field]
    assert prepared["spec"]["updateStrategy"] == {"type": "OnDelete"}
    claim = prepared["spec"]["volumeClaimTemplates"][0]
    assert claim["metadata"]["name"] == sts["metadata"]["name"]
    persistence = seaweed["spec"]["master"]["persistence"]
    assert claim["spec"]["resources"] == persistence["resources"]
    assert claim["spec"]["storageClassName"] == persistence["storageClassName"]
    master = prepared["spec"]["template"]["spec"]["containers"][0]
    assert master["image"] == sts["spec"]["template"]["spec"]["containers"][0]["image"]
    assert master["command"][2] == "weed master -mdir=/data -ip=$(POD_NAME).peers -peers=unchanged"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("wrong-owner", "ownership does not match"),
        ("existing-template", "without existing claim templates"),
        ("overlap", "overlap an existing mount"),
        ("disabled-resume", "resumption must remain enabled"),
        ("empty-command", "Unexpected master startup command"),
        ("unknown-field", "Unsupported or disabled"),
        ("terminating", "terminating controller"),
    ],
)
def test_preparation_rejects_changed_ownership_or_unexpected_state(
    resources: tuple[dict[str, Any], dict[str, Any]], case: str, message: str
) -> None:
    sts, seaweed = resources
    master = sts["spec"]["template"]["spec"]["containers"][0]
    match case:
        case "wrong-owner":
            sts["metadata"]["ownerReferences"][0]["uid"] = "another-cr"
        case "existing-template":
            sts["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "existing"}}]
        case "overlap":
            master["volumeMounts"] = [{"name": "another-volume", "mountPath": "/data"}]
        case "disabled-resume":
            master["command"][2] += " -resumeState=false"
        case "empty-command":
            master["command"] = []
        case "unknown-field":
            seaweed["spec"]["master"]["persistence"]["existingClaim"] = "shared"
        case "terminating":
            sts["metadata"]["deletionTimestamp"] = "2026-10-19T00:00:00Z"
    with pytest.raises(ValueError, match=message):
        prepare_statefulset(sts, seaweed)


@pytest.mark.parametrize("ordinal", range(3))
def test_standalone_member_matches_the_final_controller(
    resources: tuple[dict[str, Any], dict[str, Any]], ordinal: int
) -> None:
    prepared = prepare_statefulset(*resources)
    prepared["spec"]["template"]["spec"]["volumes"] = [{"name": "config", "configMap": {"name": "retained"}}]
    before = copy.deepcopy(prepared)
    claim, pod = prepare_member(prepared, ordinal)
    assert prepared == before
    name = f"store-master-{ordinal}"
    claim_name = f"store-master-{name}"
    assert claim["metadata"]["name"] == claim_name
    assert claim["metadata"]["namespace"] == "storage"
    assert claim["spec"] == prepared["spec"]["volumeClaimTemplates"][0]["spec"]
    assert pod["metadata"]["name"] == name
    assert pod["metadata"]["namespace"] == "storage"
    assert pod["metadata"]["labels"] == {
        "component": "master",
        "statefulset.kubernetes.io/pod-name": name,
        "apps.kubernetes.io/pod-index": str(ordinal),
    }
    assert "ownerReferences" not in claim["metadata"]
    assert "ownerReferences" not in pod["metadata"]
    assert "controller-revision-hash" not in pod["metadata"]["labels"]
    assert pod["spec"]["hostname"] == name
    assert pod["spec"]["subdomain"] == prepared["spec"]["serviceName"]
    assert pod["spec"]["containers"] == prepared["spec"]["template"]["spec"]["containers"]
    assert pod["spec"]["volumes"] == [
        {"name": "store-master", "persistentVolumeClaim": {"claimName": claim_name}},
        {"name": "config", "configMap": {"name": "retained"}},
    ]


@pytest.mark.parametrize("ordinal", [-1, 3])
def test_standalone_member_rejects_out_of_range_ordinals(
    resources: tuple[dict[str, Any], dict[str, Any]], ordinal: int
) -> None:
    with pytest.raises(ValueError, match="Expected an ordinal"):
        prepare_member(prepare_statefulset(*resources), ordinal)


if __name__ == "__main__":
    pytest_bazel.main()
