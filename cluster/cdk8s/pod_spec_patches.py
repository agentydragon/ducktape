"""Pod-level `Deployment` fields cdk8s_plus_34 has no typed builder for -- applied via
the `JsonPatch` escape hatch, identically across every Agentplane Deployment:

- `securityContext.seccompProfile`: `PodSecurityContextProps` has no `seccompProfile`
  field (only `ContainerSecurityContextProps` does).
- `topologySpreadConstraints`: no builder at all, pod- or container-level.
"""

from __future__ import annotations

from cdk8s import ApiObject, JsonPatch
from cdk8s_plus_34 import Deployment, k8s


def runtime_default_seccomp_patch() -> JsonPatch:
    return JsonPatch.add(
        "/spec/template/spec/securityContext/seccompProfile", k8s.SeccompProfile(type="RuntimeDefault")
    )


def topology_spread_patch(labels: dict[str, str]) -> JsonPatch:
    return JsonPatch.add(
        "/spec/template/spec/topologySpreadConstraints",
        [
            k8s.TopologySpreadConstraint(
                max_skew=1,
                topology_key="kubernetes.io/hostname",
                when_unsatisfiable="ScheduleAnyway",
                label_selector=k8s.LabelSelector(match_labels=labels),
            )
        ],
    )


def apply_pod_spec_patches(deployment: Deployment, *, labels: dict[str, str], topology_spread: bool) -> None:
    patches = [runtime_default_seccomp_patch()]
    if topology_spread:
        patches.append(topology_spread_patch(labels))
    for patch in patches:
        ApiObject.of(deployment).add_json_patch(patch)
