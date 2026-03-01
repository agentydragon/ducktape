"""Configuration models for the Home Assistant API proxy."""

import logging
import os
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator

logger = logging.getLogger(__name__)


class EntityInfo(BaseModel):
    """Registry-resolved metadata for an entity."""

    entity_id: str
    device_id: str | None = None
    area_id: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".")[0]


class Action(StrEnum):
    READ = "read"
    CONTROL = "control"


class AccessRule(BaseModel):
    read: bool = False
    control: bool = False

    def allows(self, action: Action) -> bool:
        match action:
            case Action.READ:
                return self.read
            case Action.CONTROL:
                return self.control


class Policy(BaseModel):
    all: AccessRule = AccessRule()
    domains: dict[str, AccessRule] = {}
    area_ids: dict[str, AccessRule] = {}
    device_ids: dict[str, AccessRule] = {}
    entity_ids: dict[str, AccessRule] = {}


class TokenConfig(BaseModel):
    secret: str = ""
    policy: Policy

    @model_validator(mode="after")
    def _secret_not_empty(self) -> "TokenConfig":
        if not self.secret:
            raise ValueError("token secret must not be empty")
        return self


class HomeAssistantSettings(BaseModel):
    url: str
    token: str = ""

    @model_validator(mode="after")
    def _token_not_empty(self) -> "HomeAssistantSettings":
        if not self.token:
            raise ValueError("homeassistant token must not be empty")
        return self


class Settings(BaseModel):
    homeassistant: HomeAssistantSettings
    tokens: dict[str, TokenConfig]

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        logger.info(f"Loading settings from {path.absolute()}")
        with path.open() as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"expected YAML mapping in {path}, got {type(data).__name__}")
        # Allow HA token to come from env var
        ha_token_env = os.getenv("HOMEASSISTANT_PROXY_HA_TOKEN")
        if ha_token_env:
            data.setdefault("homeassistant", {})["token"] = ha_token_env
        # Allow proxy token secrets to come from env vars
        for name, token_cfg in data.get("tokens", {}).items():
            env_key = f"HOMEASSISTANT_PROXY_TOKEN_{name.upper()}"
            env_val = os.getenv(env_key)
            if env_val:
                token_cfg["secret"] = env_val
        return cls.model_validate(data)

    @classmethod
    def from_env(cls) -> "Settings":
        path = Path(os.getenv("HOMEASSISTANT_PROXY_CONFIG", "homeassistant_proxy.yaml"))
        return cls.from_file(path)
