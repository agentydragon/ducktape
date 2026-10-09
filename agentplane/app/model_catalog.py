"""Validated model catalog configuration shared by the app and deployment renderer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.runner.harness import Harness


class ModelOption(BaseModel):
    """One model a harness may be opened with, offered to the session form."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="The route name a session opens with; opaque to the operator.")
    display_name: str = Field(description='Short human name for the session form, e.g. "Sonnet 5".')
    reasoning_efforts: list[str] = Field(
        description="Reasoning effort values supported by this model; empty means unsupported."
    )


class ModelCatalog(BaseModel):
    """The app's configuration: every model it can open a session with, and which harnesses
    accept it. A thread carries its harness and model; a sandbox is a Pod and carries neither.

    `models` holds each model's metadata once; `harnesses` references it by `model` id, so a
    model two harnesses both accept (e.g. a local Ollama route) names its display name only once.
    An empty harness list pauses its launch-form offerings, not its existing sessions
    or the low-level Sandbox Service/runner API.
    """

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
