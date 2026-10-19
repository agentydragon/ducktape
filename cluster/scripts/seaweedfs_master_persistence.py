"""Prepare persistent master manifests for a SeaweedFS operator 1.0.39 adoption.

This only writes a manifest. Rehearse it and follow the migration runbook before
stopping the operator or replacing its StatefulSet. Convert the orphaned members
before creating the final controller. This does not copy Raft files.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any


def prepare_statefulset(current_sts: dict[str, Any], seaweed_cr: dict[str, Any]) -> dict[str, Any]:
    """Add the pinned operator's persistence fields without changing member identities."""
    if current_sts["kind"] != "StatefulSet" or seaweed_cr["kind"] != "Seaweed":
        raise ValueError("Expected a StatefulSet and its owning Seaweed CR")
    metadata = current_sts["metadata"]
    owner = seaweed_cr["metadata"]
    if metadata.get("deletionTimestamp") or owner.get("deletionTimestamp"):
        raise ValueError("Refusing adoption of a terminating controller or Seaweed CR")
    if metadata["name"] != owner["name"] + "-master" or metadata["namespace"] != owner["namespace"]:
        raise ValueError("StatefulSet is not the supplied Seaweed's master controller")
    controllers = [ref for ref in metadata["ownerReferences"] if ref.get("controller")]
    if len(controllers) != 1 or controllers[0]["uid"] != owner["uid"]:
        raise ValueError("Seaweed controller ownership does not match")
    spec = current_sts["spec"]
    if spec["replicas"] != 3 or spec.get("volumeClaimTemplates"):
        raise ValueError("This adoption requires three masters without existing claim templates")
    persistence = seaweed_cr["spec"]["master"]["persistence"]
    supported = {
        "enabled",
        "mountPath",
        "storageClassName",
        "resources",
        "accessModes",
        "subPath",
        "annotations",
        "labels",
    }
    if set(persistence) - supported or not persistence["enabled"]:
        raise ValueError("Unsupported or disabled master persistence configuration")
    mount_path = persistence["mountPath"]
    if re.fullmatch(r"/[A-Za-z0-9_./-]+", mount_path) is None:
        raise ValueError("Master data path must be an absolute literal filesystem path")
    if persistence.get("accessModes", ["ReadWriteOnce"]) != ["ReadWriteOnce"]:
        raise ValueError("Each master must use an independent ReadWriteOnce claim")
    claim_name = metadata["name"]
    prepared: dict[str, Any] = {
        "apiVersion": current_sts["apiVersion"],
        "kind": "StatefulSet",
        "metadata": {
            key: copy.deepcopy(metadata[key])
            for key in ("name", "namespace", "labels", "annotations", "ownerReferences")
            if key in metadata
        },
        "spec": copy.deepcopy(spec),
    }
    prepared["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    replacement = prepared["spec"]
    containers = replacement["template"]["spec"]["containers"]
    (master,) = [container for container in containers if container["name"] == "master"]
    command = master["command"]
    if len(command) != 3 or command[:2] != ["/bin/sh", "-ec"]:
        raise ValueError("Unexpected master startup command; review the operator version")
    if " -resumeState=false" in command[2]:
        raise ValueError("Master state resumption must remain enabled")
    if " -mdir=" in command[2] or command[2].count(" -ip=") != 1:
        raise ValueError("Master command is already persistent or does not match the pinned operator")
    command[2] = command[2].replace(" -ip=", f" -mdir={mount_path} -ip=", 1)
    mounts = master.setdefault("volumeMounts", [])
    if any(volume["name"] == claim_name for volume in replacement["template"]["spec"].get("volumes", [])):
        raise ValueError("Master claim would overlap an existing Pod volume")
    if any(mount["name"] == claim_name or mount["mountPath"] == mount_path for mount in mounts):
        raise ValueError("Master persistence would overlap an existing mount")
    mounts.insert(0, {"name": claim_name, "mountPath": mount_path, "subPath": persistence.get("subPath", "")})
    claim_metadata: dict[str, Any] = {"name": claim_name}
    for key in ("annotations", "labels"):
        if key in persistence:
            claim_metadata[key] = copy.deepcopy(persistence[key])
    replacement["volumeClaimTemplates"] = [
        {
            "metadata": claim_metadata,
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": copy.deepcopy(persistence["resources"]),
                "storageClassName": persistence["storageClassName"],
            },
        }
    ]
    replacement["updateStrategy"] = {"type": "OnDelete"}
    replacement["persistentVolumeClaimRetentionPolicy"] = {"whenDeleted": "Retain", "whenScaled": "Retain"}
    return prepared


def prepare_member(prepared_sts: dict[str, Any], ordinal: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive one standalone PVC/Pod pair, for use while the controller is absent.

    No owner or revision is fabricated. The final StatefulSet adopts compatible
    Pods after every member has passed recovery and persistent restart checks.
    """
    spec = prepared_sts["spec"]
    if ordinal not in range(3) or spec["replicas"] != 3 or spec["updateStrategy"] != {"type": "OnDelete"}:
        raise ValueError("Expected an ordinal in a prepared three-member OnDelete controller")
    (template,) = spec["volumeClaimTemplates"]
    name = f"{prepared_sts['metadata']['name']}-{ordinal}"
    namespace = prepared_sts["metadata"]["namespace"]
    claim_name = f"{template['metadata']['name']}-{name}"
    claim = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            key: copy.deepcopy(template["metadata"][key])
            for key in ("labels", "annotations")
            if key in template["metadata"]
        },
        "spec": copy.deepcopy(template["spec"]),
    }
    claim["metadata"].update({"name": claim_name, "namespace": namespace})
    pod_metadata = {
        key: copy.deepcopy(spec["template"]["metadata"][key])
        for key in ("labels", "annotations", "finalizers")
        if key in spec["template"]["metadata"]
    }
    pod_metadata.update({"name": name, "namespace": namespace})
    pod_metadata.setdefault("labels", {}).update(
        {"statefulset.kubernetes.io/pod-name": name, "apps.kubernetes.io/pod-index": str(ordinal)}
    )
    pod_spec = copy.deepcopy(spec["template"]["spec"])
    pod_spec.update({"hostname": name, "subdomain": spec["serviceName"]})
    pod_spec.setdefault("volumes", []).insert(
        0, {"name": template["metadata"]["name"], "persistentVolumeClaim": {"claimName": claim_name}}
    )
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": pod_metadata, "spec": pod_spec}
    return claim, pod


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--statefulset", type=Path, required=True)
    parser.add_argument("--seaweed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--members-output", type=Path, help="Also write the three reviewed standalone PVC/Pod pairs")
    args = parser.parse_args()
    prepared = prepare_statefulset(json.loads(args.statefulset.read_text()), json.loads(args.seaweed.read_text()))
    args.output.write_text(json.dumps(prepared, indent=2) + "\n")
    if args.members_output is not None:
        args.members_output.mkdir(parents=True, exist_ok=True)
        for ordinal in range(3):
            claim, pod = prepare_member(prepared, ordinal)
            for kind, manifest in (("claim", claim), ("pod", pod)):
                (args.members_output / f"{ordinal}-{kind}.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
