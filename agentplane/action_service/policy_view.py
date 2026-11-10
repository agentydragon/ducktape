"""Typed policy views for the Action Service facade."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentplane.action_service.models import PolicyKind, PolicySource


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExactActionsView(_View):
    type: Literal[PolicyKind.EXACT_ACTIONS]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")


class ArgumentSchemaView(_View):
    type: Literal[PolicyKind.ARGUMENT_SCHEMA]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")
    argument_schema: dict[str, JsonValue] = Field(description="The JSON Schema the arguments must satisfy.")


class GitHubRepositoryView(_View):
    type: Literal[PolicyKind.GITHUB_REPOSITORY]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")
    owner: str = Field(description="The GitHub repository owner the call must target.")
    repository: str = Field(description="The GitHub repository name the call must target.")


class GitHubPublicRepositoryView(_View):
    type: Literal[PolicyKind.GITHUB_PUBLIC_REPOSITORY]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")


class HomeAssistantEntityControlView(_View):
    type: Literal[PolicyKind.HOME_ASSISTANT_ENTITY_CONTROL]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")
    entities: dict[str, list[str]] = Field(
        description="The Home Assistant entity ids a call may target, each with the services it may use, sorted."
    )


PolicyView = Annotated[
    ExactActionsView
    | ArgumentSchemaView
    | GitHubRepositoryView
    | GitHubPublicRepositoryView
    | HomeAssistantEntityControlView,
    Field(discriminator="type"),
]


class EffectivePolicyView(_View):
    """One policy as admission walks it: the binding and set it came through and its position
    there, which is what a Decision's `MatchedPolicy` names."""

    binding: str
    policy_set: str
    source: PolicySource = Field(description="The list of the set the policy sits in.")
    index: int = Field(ge=0)
    policy: PolicyView
