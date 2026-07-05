"""API models for the dispatcher (haku/dispatch/README.md)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    CREATED = "created"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


class JobRequest(BaseModel):
    prompt: str = Field(
        description=(
            "Self-contained instructions for the worker. Must pass the zone's "
            "classifier gate; the worker has no access to Haku's memory to "
            "look anything up, so all context must be inline."
        )
    )
    zone: str = Field(description="Zone name; must exist in the dispatcher's zones.yaml.")
    model: str = Field(description="Model name on the workers-LiteLLM; must be in the zone's allowlist.")
    max_budget_usd: float = Field(gt=0, le=5, description="Per-job LiteLLM key budget.")
    idempotency_key: str = Field(
        min_length=1,
        max_length=200,
        description="Caller-chosen key; the k8s Job name derives from it, so retried POSTs return the existing job.",
    )


class JobRecord(BaseModel):
    id: str = Field(description="k8s Job name, derived from the idempotency key.")
    zone: str
    model: str
    status: JobStatus
    prompt: str
    created_at: datetime
    completed_at: datetime | None
    exit_code: int | None
    result: str | None = Field(description="Worker-authored result blob — untrusted input to Haku.")


class ResultSubmission(BaseModel):
    result: str = Field(description="Contents of /output/result.md (empty if the agent produced none).")
    exit_code: int


class ClassifierVerdict(BaseModel):
    allowed: bool
    reason: str = Field(description="Shown to L0 verbatim on rejection so it can revise the brief.")


class RejectionResponse(BaseModel):
    detail: str
    verdict: ClassifierVerdict
