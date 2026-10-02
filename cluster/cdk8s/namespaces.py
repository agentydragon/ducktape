"""Ducktape's policy for the Namespace kind: the labels Goldilocks and `kyverno/policies.py` read."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Namespace, k8s
from constructs import Construct

from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS, AgentReadable

_GOLDILOCKS_ENABLED_LABEL = "goldilocks.fairwinds.com/enabled"
VPA_UPDATE_MODE_LABEL = "goldilocks.fairwinds.com/vpa-update-mode"


class Vpa(StrEnum):
    """The VPA Goldilocks keeps for the namespace's workloads. Every member but DISABLED is that
    VPA's update mode (RECOMMEND only records recommendations), labeled alone: Goldilocks runs
    on by default (`goldilocks.py`), so only DISABLED labels the namespace `enabled: "false"`."""

    DISABLED = "disabled"
    RECOMMEND = "off"
    INITIAL = "initial"
    AUTO = "auto"


_POLICY_LABELS = frozenset({_GOLDILOCKS_ENABLED_LABEL, VPA_UPDATE_MODE_LABEL, *AgentReadable})


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
    vpa: Vpa,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
) -> Namespace:
    """A Namespace labeled for `vpa` and the shared diagnostics policy."""
    return Namespace(
        scope,
        id,
        metadata=ApiObjectMetadata(
            name=name, labels=_labels(name, vpa, labels), annotations=None if annotations is None else dict(annotations)
        ),
    )


def namespace_patch(
    scope: Construct, id: str, *, name: str, vpa: Vpa, labels: Mapping[str, str] | None = None
) -> k8s.KubeNamespace:
    """A strategic-merge patch labeling an upstream release's Namespace `name` as `namespace`
    would. Tier 2, since `Namespace` renders `spec: {}`, which the patch would add to the object."""
    return k8s.KubeNamespace(scope, id, metadata=k8s.ObjectMeta(name=name, labels=_labels(name, vpa, labels)))
