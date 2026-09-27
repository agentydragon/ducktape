"""Cluster pod policy: `harden` and `place`, each called once on a finished workload of any
dialect -- a cdk8s-plus workload, a raw `k8s.Kube*` object, or a CRD that embeds a pod
template. Both reach the pod spec through `POD_SPEC_PATHS`, which the fleet rules also
read, and patch in only what it leaves unset, so a value stated at construction wins.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from cdk8s import ApiObject, JsonPatch
from cdk8s_plus_34 import k8s
from constructs import Construct

from cluster.cdk8s.node_scheduling import CONTROL_PLANE_TOLERATION, Placement

POD_SPEC_PATHS: Mapping[str, str] = MappingProxyType(
    {
        "Deployment": "/spec/template/spec",
        "StatefulSet": "/spec/template/spec",
        "DaemonSet": "/spec/template/spec",
        "ReplicaSet": "/spec/template/spec",
        "Job": "/spec/template/spec",
        "CronJob": "/spec/jobTemplate/spec/template/spec",
        "SandboxTemplate": "/spec/podTemplate/spec",
        "ScaledJob": "/spec/jobTargetRef/template/spec",
    }
)
_RUNTIME_DEFAULT = k8s.SeccompProfile(type="RuntimeDefault")


def pod_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    """The pod spec inside a rendered workload; `KeyError` for a kind `POD_SPEC_PATHS` lacks."""
    spec = manifest
    for key in POD_SPEC_PATHS[manifest["kind"]].split("/")[1:]:
        spec = spec[key]
    return spec


def _workload(workload: Construct) -> tuple[ApiObject, str, dict[str, Any]]:
    obj = ApiObject.of(workload)
    return obj, POD_SPEC_PATHS[obj.kind], pod_spec(obj.to_json())


def harden(workload: Construct) -> None:
    """RuntimeDefault seccomp on the Pod and `allowPrivilegeEscalation: false` on each init and
    main container, where unset. Call it once every container exists."""
    obj, path, pod = _workload(workload)
    if "securityContext" not in pod:
        obj.add_json_patch(
            JsonPatch.add(f"{path}/securityContext", k8s.PodSecurityContext(seccomp_profile=_RUNTIME_DEFAULT))
        )
    elif "seccompProfile" not in pod["securityContext"]:
        obj.add_json_patch(JsonPatch.add(f"{path}/securityContext/seccompProfile", _RUNTIME_DEFAULT))
    for field in ("initContainers", "containers"):
        for index, container in enumerate(pod.get(field, [])):
            at = f"{path}/{field}/{index}/securityContext"
            if "securityContext" not in container:
                obj.add_json_patch(JsonPatch.add(at, k8s.SecurityContext(allow_privilege_escalation=False)))
            elif "allowPrivilegeEscalation" not in container["securityContext"]:
                obj.add_json_patch(JsonPatch.add(f"{at}/allowPrivilegeEscalation", False))


def place(workload: Construct, placement: Placement, *, tolerate_control_plane: bool = False) -> None:
    """Requires the nodes `placement` selects, as a required node affinity; with
    `tolerate_control_plane`, control-plane nodes among them qualify too. Raises on a pod spec
    that already states a node affinity or selector."""
    obj, path, pod = _workload(workload)
    if "nodeSelector" in pod or "affinity" in pod:
        raise ValueError(f"{obj.kind}/{obj.name}: already placed")
    required = k8s.NodeSelectorTerm(
        match_expressions=[
            k8s.NodeSelectorRequirement(key=label, operator="In", values=[value])
            for label, value in placement.node_selector.items()
        ]
    )
    obj.add_json_patch(
        JsonPatch.add(
            f"{path}/affinity",
            k8s.Affinity(
                node_affinity=k8s.NodeAffinity(
                    required_during_scheduling_ignored_during_execution=k8s.NodeSelector(node_selector_terms=[required])
                )
            ),
        )
    )
    if tolerate_control_plane:
        obj.add_json_patch(
            JsonPatch.add(f"{path}/tolerations/-", CONTROL_PLANE_TOLERATION)
            if "tolerations" in pod
            else JsonPatch.add(f"{path}/tolerations", [CONTROL_PLANE_TOLERATION])
        )
