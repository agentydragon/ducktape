"""Shared Action Service policy and operator identity types."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OperatorPrincipal(BaseModel):
    """The human or BFF behind the operator surface, as its adapter verified them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)


class PolicyKind(StrEnum):
    """The wire `type` of one Action policy kind."""

    EXACT_ACTIONS = "exact_actions"
    ARGUMENT_SCHEMA = "argument_schema"
    GITHUB_REPOSITORY = "github_repository"
    GITHUB_PUBLIC_REPOSITORY = "github_public_repository"
    HOME_ASSISTANT_ENTITY_CONTROL = "home_assistant_entity_control"


class PolicySource(StrEnum):
    """Which PolicySet list contains a policy."""

    RULES = "rules"
