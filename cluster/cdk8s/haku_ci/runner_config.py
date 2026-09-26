"""The fields of forgejo-runner's config file that haku-ci sets; every other field keeps the
runner's default. Upstream reference: the runner's `internal/pkg/config/config.example.yaml`."""

from __future__ import annotations

import yaml
from pydantic import BaseModel, Field, field_serializer


class Log(BaseModel):
    level: str


class Runner(BaseModel):
    file: str
    capacity: int
    timeout: int = Field(description="Seconds.")
    shutdown_timeout: int = Field(description="Seconds.")
    labels: list[str]

    @field_serializer("timeout", "shutdown_timeout")
    def _go_duration(self, seconds: int) -> str:
        # The runner decodes these into Go `time.Duration`, which reads a bare YAML integer as
        # nanoseconds.
        return f"{seconds}s"


class Container(BaseModel):
    docker_host: str
    options: str = Field(description="`docker run` flags, split by the runner with go-shellquote.")
    valid_volumes: list[str]


class Cache(BaseModel):
    enabled: bool


class Config(BaseModel):
    log: Log
    runner: Runner
    container: Container
    cache: Cache

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(), sort_keys=False)
