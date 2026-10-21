"""Operator consent to an Actions-owned OAuth enrollment, relayed to the Action Service.

The Action Service pins an enrollment to the operator of its first preview and records one decision
on it. The app keeps no consent state: a decision whose answer is lost cannot be made again, and the
operator restarts the authorization from their MCP client.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.action_service.connections import Connection
from agentplane.action_service.enrollments import (
    EnrollmentAllow,
    EnrollmentConnection,
    EnrollmentDecisionResult,
    EnrollmentDeny,
    EnrollmentPreview,
    EnrollmentPreviewInput,
)
from agentplane.subjects import ServiceAccountRef

EnrollmentHandle = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
# CLEANUP(added 2026-10-21): Remove once the app's consent calls the Go ConnectionService, whose requests carry no
#   browser binding.
# The Python Action Service requires a browser binding and refuses any but the first preview's: one value for every
# browser leaves only its check of the operator.
_BROWSER_BINDING = "agentplane-app-consent-binds-no-browser"


class ConsentAllow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["allow"]
    expected_version: int = Field(ge=1, description="The preview's `enrollment.version`.")
    connection: EnrollmentConnection
    service_account: ServiceAccountRef


class ConsentDeny(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["deny"]
    expected_version: int = Field(ge=1, description="The preview's `enrollment.version`.")


ConsentDecision = Annotated[ConsentAllow | ConsentDeny, Field(discriminator="verdict")]


class ConsentPreview(BaseModel):
    enrollment: EnrollmentPreview
    service_accounts: list[ServiceAccountRef] = Field(
        description="The labeled caller ServiceAccounts the Action Service currently sees; the picker's choices."
    )
    connections: list[Connection]


async def preview_enrollment(handle: str, client: OperatorActionServiceClient) -> ConsentPreview:
    return ConsentPreview(
        enrollment=await client.preview_enrollment(handle, EnrollmentPreviewInput(browser_binding=_BROWSER_BINDING)),
        service_accounts=await client.caller_service_accounts(),
        connections=await client.connections(),
    )


async def decide_enrollment(
    handle: str, body: ConsentDecision, client: OperatorActionServiceClient
) -> EnrollmentDecisionResult:
    # The Python Action Service answers a repeat of the whole request, key included, with the recorded result: a fresh
    # key leaves every decision after the first refused.
    relayed = {
        "browser_binding": _BROWSER_BINDING,
        "expected_version": body.expected_version,
        "idempotency_key": str(uuid4()),
    }
    decision = (
        EnrollmentAllow.model_validate(
            {**relayed, "connection": body.connection, "service_account": body.service_account}
        )
        if isinstance(body, ConsentAllow)
        else EnrollmentDeny.model_validate(relayed)
    )
    # This URL is the authenticated authority's server-held continuation, never browser metadata.
    return await client.decide_enrollment(handle, decision)
