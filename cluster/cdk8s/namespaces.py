"""Ducktape's policy for the Namespace kind: the labels Goldilocks and `kyverno/policies.py` read."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from cdk8s_plus_34 import k8s
from constructs import Construct

_GOLDILOCKS_ENABLED_LABEL = "goldilocks.fairwinds.com/enabled"
VPA_UPDATE_MODE_LABEL = "goldilocks.fairwinds.com/vpa-update-mode"


class AgentReadable(StrEnum):
    """What agents may read in the namespace (`cluster/docs/agent_rbac.md`): the label key
    `kyverno/policies.py` generates the agent RoleBindings from. LOGS includes METADATA."""

    METADATA = "rbac.ducktape.io/agent-readable-metadata"
    LOGS = "rbac.ducktape.io/agent-readable-logs"


class Vpa(StrEnum):
    """The VPA Goldilocks keeps for the namespace's workloads. Every member but DISABLED is that
    VPA's update mode (RECOMMEND only records recommendations), rendered beside
    `enabled: "true"`, which `cluster/validation/checks.py` requires with an update mode."""

    DISABLED = "disabled"
    RECOMMEND = "off"
    INITIAL = "initial"
    AUTO = "auto"


def namespace(
    scope: Construct,
    id: str,
    *,
    name: str,
    vpa: Vpa,
    agent_readable: AgentReadable | None,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
) -> k8s.KubeNamespace:
    """A Namespace labeled for `vpa` and `agent_readable`; `labels` carries any others."""
    policy: dict[str, str] = (
        {_GOLDILOCKS_ENABLED_LABEL: "false"}
        if vpa is Vpa.DISABLED
        else {_GOLDILOCKS_ENABLED_LABEL: "true", VPA_UPDATE_MODE_LABEL: vpa}
    )
    if agent_readable is not None:
        policy[agent_readable] = "true"
    extra = labels or {}
    if overlap := policy.keys() & extra.keys():
        raise ValueError(f"{name=}: {sorted(overlap)} are set by vpa and agent_readable, not labels")
    return k8s.KubeNamespace(
        scope,
        id,
        metadata=k8s.ObjectMeta(
            name=name, labels={**extra, **policy}, annotations=None if annotations is None else dict(annotations)
        ),
    )
