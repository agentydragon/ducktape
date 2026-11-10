"""Shared Python models and API names for policy resources (`agentplane/crds/manifests/`).

The Go runtime reads PolicySets. Expiry is not a reader's concern: the Policy Controller deletes a
binding once its `expiresAt` passes, so a binding that exists grants.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

POLICY_SETS_PLURAL = "policysets"
POLICY_BINDINGS_PLURAL = "policybindings"
# Flux stamps this label on objects it owns and will reapply after runtime deletion.
FLUX_KUSTOMIZATION_LABEL = "kustomize.toolkit.fluxcd.io/name"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True, alias_generator=to_camel)


class ConditionStatus(StrEnum):
    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"


class Condition(_Wire):
    """A metav1.Condition."""

    type: str
    status: ConditionStatus
    reason: str
    message: str
    observed_generation: int | None = None
    last_transition_time: AwareDatetime
