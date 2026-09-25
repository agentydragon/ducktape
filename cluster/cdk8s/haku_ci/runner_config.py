"""The fields of forgejo-runner's config file that haku-ci sets; every other field keeps the
runner's default. Upstream reference: the runner's `internal/pkg/config/config.example.yaml`."""

from __future__ import annotations

import shlex
from datetime import timedelta

import yaml
from pydantic import BaseModel, field_serializer


class Log(BaseModel):
    level: str


class Runner(BaseModel):
    file: str
    capacity: int
    timeout: timedelta
    shutdown_timeout: timedelta
    labels: list[str]

    @field_serializer("timeout", "shutdown_timeout")
    def _go_duration(self, value: timedelta) -> str:
        return f"{int(value.total_seconds())}s"


class Container(BaseModel):
    docker_host: str
    options: list[str]
    valid_volumes: list[str]

    @field_serializer("options")
    def _shell_joined(self, value: list[str]) -> str:
        # The runner takes one string and splits it with go-shellquote (POSIX shell rules), which
        # is what shlex.join quotes for.
        return shlex.join(value)


class Cache(BaseModel):
    enabled: bool


class Config(BaseModel):
    log: Log
    runner: Runner
    container: Container
    cache: Cache

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(), sort_keys=False)
