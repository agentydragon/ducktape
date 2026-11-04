"""Bearer authentication for session owners and route-limited consumers."""

from __future__ import annotations

import hmac
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from starlette.requests import Request

from platform_sessions.routes import Route


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instance: str
    method: str
    path: str


class BearerConfig(BaseModel):
    """Configure a bearer inline or load it from a mounted secret file."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    token: SecretStr | None = Field(default=None, repr=False)
    token_file: Path | None = None
    role: Literal["owner", "routes"]
    allow: list[RouteConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source(self) -> BearerConfig:
        if (self.token is None) == (self.token_file is None):
            raise ValueError("Set exactly one of token or token_file")
        if self.role == "owner" and self.allow:
            raise ValueError("Owner bearer does not need route rules")
        return self

    def load_token(self) -> SecretStr:
        if self.token is not None:
            token = self.token
        else:
            assert self.token_file is not None
            token = SecretStr(self.token_file.read_text().strip())
        if len(token.get_secret_value()) < 32:
            raise ValueError("Bearer secret must contain at least 32 characters")
        return token


@dataclass(frozen=True)
class Bearer:
    token: SecretStr = field(repr=False)
    owner: bool = False
    routes: Mapping[str, tuple[Route, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.owner and self.routes:
            raise ValueError("Owner bearer does not need route rules")


class BearerAuth:
    def __init__(self, bearers: Sequence[Bearer]):
        self.bearers = tuple(bearers)
        tokens = [bearer.token.get_secret_value() for bearer in bearers]
        if any(not token for token in tokens) or len(set(tokens)) != len(tokens):
            raise ValueError("Empty or duplicate bearer credential")

    @classmethod
    def from_config(cls, configs: Mapping[str, BearerConfig], *, instances: Collection[str]) -> BearerAuth:
        bearers = []
        for config in configs.values():
            routes: dict[str, list[Route]] = defaultdict(list)
            for item in config.allow:
                if item.instance not in instances:
                    raise ValueError("Bearer route references an unknown or disabled integration")
                routes[item.instance].append(Route(item.method, item.path))
            bearers.append(
                Bearer(
                    config.load_token(),
                    owner=config.role == "owner",
                    routes={instance: tuple(rules) for instance, rules in routes.items()},
                )
            )
        return cls(bearers)

    def authenticate(self, request: Request) -> Bearer | None:
        auth = [value for name, value in request.scope["headers"] if name.lower() == b"authorization"]
        if len(auth) != 1:
            return None
        scheme, separator, token = auth[0].partition(b" ")
        if not separator or scheme.lower() != b"bearer":
            return None
        principal = None
        for bearer in self.bearers:
            if hmac.compare_digest(token, bearer.token.get_secret_value().encode("utf-8")):
                principal = bearer
        return principal
