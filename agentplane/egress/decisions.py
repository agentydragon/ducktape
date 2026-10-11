"""Admission metadata, captured before forwarding; never upstream completion evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentplane.egress.policy import DenyReason
from agentplane.subjects import ServiceAccountRef


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Phase(StrEnum):
    CONNECT = "connect"
    HTTP_REQUEST = "http_request"


class Decision(BaseModel):
    """One admission as a reader of the admin port's `/decisions` sees it; names only, never values.

    Paths are deliberately never captured: arbitrary path segments can contain secrets.
    """

    # The admin port serves whole `DecisionRecord`s, and a reader deployed apart from the proxy may
    # meet fields a newer proxy added first; it keeps only these.
    model_config = ConfigDict(extra="ignore", frozen=True)

    at: datetime
    method: str
    host: str
    port: int
    outcome: Outcome
    reason: str | None = Field(default=None, description="The proxy's machine-readable refusal, on a deny.")
    binding: str | None = None
    policy: str | None = None
    rule: int | None = None
    substituted: bool = Field(default=False, description="Whether a credential replaced a placeholder.")
    address: str | None = Field(default=None, description="The address the host was pinned to, when admitted.")


class DecisionRecord(Decision):
    """One admission as the proxy logs and persists it.

    `producer_id`, `source_pod_uid`, `connection_id` and `phase` have no reader in Agentplane, nor
    does the `egress_decision.ingested_at` column: they are kept for operators querying the table
    directly, to tell replicas, Pods and connections apart and to measure ingestion lag.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    producer_id: UUID
    source_pod_uid: str | None = None
    connection_id: str
    phase: Phase
    subject: ServiceAccountRef | None = Field(
        description="The ServiceAccount the token proved; absent on a refusal that never authenticated."
    )
    reason: DenyReason | None = None
