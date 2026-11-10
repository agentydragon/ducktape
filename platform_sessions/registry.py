"""Explicit integration types and validated instance configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"


class IntegrationConfigError(ValueError):
    """Configuration is invalid; error messages never contain settings values."""


class InstanceConfig(BaseModel):
    """Non-secret inputs for a configured integration instance."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: str = Field(pattern=rf"^{_ID_PATTERN}$")
    type: str
    enabled: bool = True
    upstream_base_url: str | None = None
    state_dir: str | None = None
    poll_interval_seconds: int = Field(default=900, gt=0)
    settings: dict[str, Any] = Field(default_factory=dict, repr=False)


class HostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    integrations: list[InstanceConfig]


@dataclass(frozen=True)
class IntegrationType:
    """A type is registered by application code, never imported from config."""

    name: str
    settings_model: type[BaseModel]
    default_upstream_base_url: str


@dataclass(frozen=True)
class IntegrationInstance:
    id: str
    type: str
    enabled: bool
    upstream_base_url: str
    state_dir: PurePosixPath
    poll_interval_seconds: int
    settings: BaseModel = field(repr=False)


def _upstream_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            value.isascii()
            and "\\" not in value
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            and parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        # Accessing port rejects malformed and out-of-range values.
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise IntegrationConfigError("Invalid upstream base URL")
    return value.rstrip("/")


class IntegrationRegistry:
    def __init__(self, types: tuple[IntegrationType, ...]):
        self._types: dict[str, IntegrationType] = {}
        for integration_type in types:
            if not re.fullmatch(_ID_PATTERN, integration_type.name) or integration_type.name in self._types:
                raise IntegrationConfigError("Invalid or duplicate integration type")
            _upstream_url(integration_type.default_upstream_base_url)
            self._types[integration_type.name] = integration_type

    def resolve(self, value: object) -> tuple[IntegrationInstance, ...]:
        """Validate all entries before returning any resolved instances."""
        config = HostConfig.model_validate(value)
        instances = []
        ids: set[str] = set()
        directories: set[PurePosixPath] = set()
        for item in config.integrations:
            if item.id in ids:
                raise IntegrationConfigError("Duplicate integration ID")
            ids.add(item.id)
            integration_type = self._types.get(item.type)
            if integration_type is None:
                raise IntegrationConfigError("Unknown integration type")
            state_dir = PurePosixPath(item.state_dir if item.state_dir is not None else f"/data/{item.id}")
            if not state_dir.is_absolute() or ".." in state_dir.parts or state_dir in directories:
                raise IntegrationConfigError("Invalid or duplicate integration state directory")
            directories.add(state_dir)
            settings = integration_type.settings_model.model_validate(item.settings)
            instances.append(
                IntegrationInstance(
                    id=item.id,
                    type=item.type,
                    enabled=item.enabled,
                    upstream_base_url=_upstream_url(
                        item.upstream_base_url
                        if item.upstream_base_url is not None
                        else integration_type.default_upstream_base_url
                    ),
                    state_dir=state_dir,
                    poll_interval_seconds=item.poll_interval_seconds,
                    settings=settings,
                )
            )
        return tuple(instances)
