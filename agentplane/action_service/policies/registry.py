"""The `type`-discriminated union of Action policy models that a set's lists hold."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from agentplane.action_service.policies import (
    argument_schema,
    exact_actions,
    github_public_repository,
    github_repository,
    home_assistant_entity_control,
)

Policy = Annotated[
    exact_actions.ExactActions
    | argument_schema.ArgumentSchema
    | github_repository.GitHubRepository
    | github_public_repository.GitHubPublicRepository
    | home_assistant_entity_control.HomeAssistantEntityControl,
    Field(discriminator="type"),
]
