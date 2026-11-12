"""File-only configuration for the local conversation experiment."""

import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, DirectoryPath, Field, FilePath, model_validator


class Mode(StrEnum):
    AUTHORED = "authored"
    CHAT = "chat"


class EnvironmentCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["environment"]
    name: str = Field(min_length=1)

    def read(self) -> str:
        return os.environ[self.name]


class FileCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["file"]
    path: FilePath

    def read(self) -> str:
        return self.path.read_text().strip()


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    credential: Annotated[EnvironmentCredential | FileCredential, Field(discriminator="kind")]
    workspace: DirectoryPath
    state_directory: Path
    mode: Mode = Mode.AUTHORED
    turn_control: Literal["implicit", "yield_tool"] = "implicit"
    max_steps: int = Field(default=24, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0, ge=0, le=2)
    request_timeout_seconds: float = Field(default=120, gt=0)
    command_timeout_seconds: float = Field(default=15, gt=0)
    max_tool_output_bytes: int = Field(default=16_384, gt=0)
    max_message_chars: int = Field(default=8000, gt=0)
    thread_page_size: int = Field(default=20, gt=0, le=100)

    @model_validator(mode="after")
    def validate_turn_control(self) -> Self:
        if self.turn_control == "yield_tool" and self.mode is Mode.CHAT:
            raise ValueError("yield_tool turn control requires authored mode")
        return self
