"""Shared Action Service policy types."""

from enum import StrEnum


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
