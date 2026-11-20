"""Render a VPA PodResourcePolicy into Goldilocks' resource-policy annotation."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

VPA_RESOURCE_POLICY_ANNOTATION = "goldilocks.fairwinds.com/vpa-resource-policy"


class ControlledValues(StrEnum):
    REQUESTS_ONLY = "RequestsOnly"
    REQUESTS_AND_LIMITS = "RequestsAndLimits"


class ContainerPolicy(BaseModel):
    """Explicit VPA ownership and optional bounds for one container or the wildcard."""

    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, extra="forbid", frozen=True)

    container_name: str = "*"
    controlled_values: ControlledValues
    min_allowed: dict[str, str] | None = None
    max_allowed: dict[str, str] | None = None
    controlled_resources: tuple[Literal["cpu", "memory"], ...] | None = None
    mode: Literal["Auto", "Off"] | None = None


REQUESTS_ONLY = ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY)


def resource_policy_annotation(*container_policies: ContainerPolicy) -> str:
    """Serialize a complete policy. A workload annotation replaces the namespace policy."""
    return json.dumps(
        {
            "containerPolicies": [
                policy.model_dump(mode="json", by_alias=True, exclude_none=True) for policy in container_policies
            ]
        }
    )
