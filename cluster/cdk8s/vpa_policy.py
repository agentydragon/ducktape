"""Render a VPA PodResourcePolicy into Goldilocks' resource-policy annotation."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Literal, Self

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

    @classmethod
    def cpu_and_memory_requests_only(
        cls, container_name: str, *, cpu_floor: str | None = None, memory_floor: str | None = None
    ) -> Self:
        """Size both requests, with floors where the workload needs a reservation."""
        floors = {
            resource: floor for resource, floor in (("cpu", cpu_floor), ("memory", memory_floor)) if floor is not None
        }
        return cls(
            container_name=container_name,
            controlled_values=ControlledValues.REQUESTS_ONLY,
            controlled_resources=("cpu", "memory"),
            min_allowed=floors or None,
        )

    @classmethod
    def memory_requests_only(
        cls, container_name: str, *, cpu_request: str, memory_floor: str | None = None, memory_limit: str | None = None
    ) -> Self:
        """Size memory requests and pin CPU against recommendations cached before opt-in.

        Optional memory bounds apply to the VPA request. The workload owns the container limit.
        """
        min_allowed = {"cpu": cpu_request}
        max_allowed = {"cpu": cpu_request}
        if memory_floor is not None:
            min_allowed["memory"] = memory_floor
        if memory_limit is not None:
            max_allowed["memory"] = memory_limit
        return cls(
            container_name=container_name,
            controlled_values=ControlledValues.REQUESTS_ONLY,
            controlled_resources=("memory",),
            min_allowed=min_allowed,
            max_allowed=max_allowed,
        )


REQUESTS_ONLY = ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY)


def resource_policy_annotation(*container_policies: ContainerPolicy, compact: bool = False) -> str:
    """Serialize a complete policy. A workload annotation replaces the namespace policy."""
    return json.dumps(
        {
            "containerPolicies": [
                policy.model_dump(mode="json", by_alias=True, exclude_none=True) for policy in container_policies
            ]
        },
        separators=(",", ":") if compact else None,
    )
