"""Authentication allowlist settings for one service listener."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KubernetesBearerAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["kubernetes_bearer"] = "kubernetes_bearer"
    audience: str = Field(min_length=1)


type Authentication = KubernetesBearerAuth


class Listener(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listen_address: str = Field(min_length=1)
    auth: list[Authentication] = Field(min_length=1)

    @model_validator(mode="after")
    def authentication_modes(self) -> Self:
        modes = [method.mode for method in self.auth]
        if len(set(modes)) != len(modes):
            raise ValueError("duplicate authentication mode")
        return self
