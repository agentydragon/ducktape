"""Bearer authentication for provider sessions and route-limited consumers."""

from __future__ import annotations

import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import SecretStr
from starlette.requests import Request

from platform_sessions.routes import Route


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
