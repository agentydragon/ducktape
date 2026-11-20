"""Ducktape's policy for the Namespace kind: the labels Goldilocks and `kyverno/policies.py` read."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Namespace, k8s
from constructs import Construct

from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS, AgentReadable
from cluster.cdk8s.vpa_policy import (
    REQUESTS_ONLY,
    VPA_RESOURCE_POLICY_ANNOTATION,
    ContainerPolicy,
    resource_policy_annotation,
)

_GOLDILOCKS_ENABLED_LABEL = "goldilocks.fairwinds.com/enabled"
VPA_UPDATE_MODE_LABEL = "goldilocks.fairwinds.com/vpa-update-mode"
_VPA_MIN_REPLICAS_ANNOTATION = "goldilocks.fairwinds.com/vpa-min-replicas"


class Vpa(StrEnum):
    """The VPA Goldilocks keeps for the namespace's workloads. Every member but DISABLED is that
    VPA's update mode (RECOMMEND only records recommendations), labeled alone: Goldilocks runs
    on by default (`goldilocks.py`), so only DISABLED labels the namespace `enabled: "false"`."""

    DISABLED = "disabled"
    RECOMMEND = "off"
    INITIAL = "initial"
    AUTO = "auto"


@dataclass(frozen=True)
class VpaProfile:
    """Explicit namespace policy, with the optional updater minimum shared by all of its VPAs."""

    mode: Vpa
    container_policies: tuple[ContainerPolicy, ...] = ()
    min_replicas: int | None = None

    @classmethod
    def requests_only(cls, mode: Vpa, *, min_replicas: int | None = None) -> Self:
        return cls(mode, (REQUESTS_ONLY,), min_replicas=min_replicas)

    def annotations(self) -> dict[str, str]:
        annotations = {}
        if self.container_policies:
            annotations[VPA_RESOURCE_POLICY_ANNOTATION] = resource_policy_annotation(*self.container_policies)
        if self.min_replicas is not None:
            annotations[_VPA_MIN_REPLICAS_ANNOTATION] = str(self.min_replicas)
        return annotations


_POLICY_LABELS = {_GOLDILOCKS_ENABLED_LABEL, VPA_UPDATE_MODE_LABEL, *AgentReadable}


def _labels(name: str, vpa: Vpa, labels: Mapping[str, str] | None) -> dict[str, str]:
    policy: dict[str, str] = (
        {_GOLDILOCKS_ENABLED_LABEL: "false"} if vpa is Vpa.DISABLED else {VPA_UPDATE_MODE_LABEL: vpa}
    )
    if (agent_readable := NAMESPACE_DIAGNOSTICS.get(name)) is not None:
        policy[agent_readable] = "true"
    extra = labels or {}
    if overlap := extra.keys() & _POLICY_LABELS:
        raise ValueError(f"{name=}: {sorted(overlap)} are set by vpa and namespace_access, not labels")
    return {**extra, **policy}


def namespace(
    scope: Construct,
    id: str,
    *,
    name: str,
    vpa: Vpa | VpaProfile,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
) -> Namespace:
    """A Namespace labeled for `vpa` and the shared diagnostics policy."""
    metadata_annotations = None if annotations is None else dict(annotations)
    if isinstance(vpa, VpaProfile):
        vpa_annotations = vpa.annotations()
        metadata_annotations = metadata_annotations or {}
        if overlap := metadata_annotations.keys() & vpa_annotations.keys():
            raise ValueError(f"{name=}: {sorted(overlap)} are set by the VPA profile")
        metadata_annotations.update(vpa_annotations)
        vpa = vpa.mode
    return Namespace(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, labels=_labels(name, vpa, labels), annotations=metadata_annotations),
    )


def namespace_patch(
    scope: Construct, id: str, *, name: str, vpa: Vpa, labels: Mapping[str, str] | None = None
) -> k8s.KubeNamespace:
    """A strategic-merge patch labeling an upstream release's Namespace `name` as `namespace`
    would. Tier 2, since `Namespace` renders `spec: {}`, which the patch would add to the object."""
    return k8s.KubeNamespace(scope, id, metadata=k8s.ObjectMeta(name=name, labels=_labels(name, vpa, labels)))
