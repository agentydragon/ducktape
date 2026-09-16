"""Admission metadata, captured before forwarding; never upstream completion evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from x.agentplane.egress.policy import DenyReason


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Phase(StrEnum):
    CONNECT = "connect"
    HTTP_REQUEST = "http_request"


class DecisionRecord(BaseModel):
    """One admission as served on the admin port and logged; carries names, never values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    producer_id: UUID
    sandbox_namespace: str | None = None
    sandbox_uid: str | None = None
    source_pod_uid: str | None = None
    connection_id: str
    phase: Phase
    at: datetime
    sandbox: str | None = Field(description="The Sandbox subject; absent unless a Sandbox owns the Pod.")
    service_account: str | None = Field(
        default=None, description="The ServiceAccount subject; absent unless the Pod is owned by no Sandbox."
    )
    method: str
    host: str
    port: int
    path: None = Field(
        default=None, description="Paths are deliberately omitted: arbitrary path segments can contain secrets."
    )
    outcome: Outcome
    reason: DenyReason | None = None
    binding: str | None = None
    policy: str | None = None
    rule: int | None = None
    substituted: bool = Field(default=False, description="Whether a credential replaced a placeholder.")
    address: str | None = Field(default=None, description="The address the host was pinned to, when admitted.")
