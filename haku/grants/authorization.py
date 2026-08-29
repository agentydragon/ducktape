"""Shared effective-authorization result vocabulary.

An authorization result says whether a request may proceed and, when it may,
where that authority is declared.  Individual domains add only the payload
needed to execute their decision.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class GrantSourceKind(StrEnum):
    """The authority's durable declaration location."""

    CONFIG_FILE = "config_file"
    DATABASE = "database"


class AuthorizationUnavailableError(RuntimeError):
    """A required authorization authority could not be evaluated."""


class AuthorizationAllowed(BaseModel):
    """An authorization decision admitted by one effective authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: Literal[True] = True
    source: GrantSourceKind
    decision_id: str = Field(min_length=1)
    reason: str | None = None
    valid_until: AwareDatetime | None = None


class AuthorizationDenied(BaseModel):
    """An authorization decision that no effective authority admitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: Literal[False] = False
    reason: str = Field(min_length=1)


type AuthorizationDecision = AuthorizationAllowed | AuthorizationDenied
