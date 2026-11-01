"""Deployment-published runtime choices shared by the app and target discovery."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.env.runtime.harnesshost.harness import Harness


class Provenance(StrEnum):
    DEPLOYMENT_CONFIG = "DEPLOYMENT_CONFIG"


class ModelOption(BaseModel):
    """One deployment-configured model offer."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="The route name a session opens with; opaque to the operator.")
    display_name: str = Field(description='Short human name for the session form, e.g. "Sonnet 5".')
    reasoning_efforts: list[str] = Field(
        description="Reasoning effort values configured for this model; empty means unsupported."
    )


class ModelCatalog(BaseModel):
    """Configured model routes and the harnesses that can use them."""

    model_config = ConfigDict(extra="forbid")

    models: list[ModelOption]
    harnesses: dict[Harness, list[str]]

    @model_validator(mode="after")
    def _check_consistency(self) -> ModelCatalog:
        ids = [option.model for option in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError(f"ModelCatalog.models has duplicate model ids: {ids}")
        known = set(ids)
        for option in self.models:
            if len(option.reasoning_efforts) != len(set(option.reasoning_efforts)):
                raise ValueError(f"Model {option.model!r} has duplicate reasoning efforts")
        for harness, referenced in self.harnesses.items():
            if unknown := [model for model in referenced if model not in known]:
                raise ValueError(f"{harness} references models outside ModelCatalog.models: {unknown}")
        return self


class TemplateRuntimeConfig(BaseModel):
    """Facts tied to one template actually rendered by deployment config."""

    model_config = ConfigDict(extra="forbid")

    workspace_root: str | None = None
    workspace_provenance: Provenance | None = None
    workspace_root_enforced: bool = False
    creates_missing_cwd: bool = False


class Catalog(BaseModel):
    """Runtime offers plus the templates to which deployment guidance applies."""

    model_config = ConfigDict(extra="forbid")

    models: ModelCatalog
    templates: dict[str, TemplateRuntimeConfig]


WORKSPACE_ROOT = "/state/workspaces"
