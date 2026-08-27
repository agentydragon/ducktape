"""FastAPI boundaries for Haku's Operator-authenticated Agent enrollment ceremony."""

from __future__ import annotations

import datetime
from typing import Annotated, Literal, Never, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import RedirectResponse, Response

from haku.console.agents.enrollment import (
    AccessProfileUnavailableError,
    AgentAccessProfileManagedByDeploymentError,
    AgentEnrollmentService,
    AgentNameUnavailableError,
    AgentNotFoundError,
    CreateAgentDecision,
    DenyEnrollmentDecision,
    EnrollmentAllowed,
    EnrollmentBrowserBindingError,
    EnrollmentBrowserSession,
    EnrollmentDecision,
    EnrollmentDecisionConflictError,
    EnrollmentDenied,
    EnrollmentInteractionExpiredError,
    EnrollmentInteractionNotFoundError,
    EnrollmentPage,
    OperatorAgent,
    ReconnectAgentDecision,
)
from haku.console.agents.models import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.agents.naming import InvalidAgentNameError
from haku.console.config import Settings
from haku.console.operator_agents import AgentEnrollmentServiceDep
from haku.console.operator_auth import OperatorSession, operator_session

entry_router = APIRouter(prefix="/auth/agent-enrollment", tags=["agent-enrollment"])
operator_router = APIRouter(prefix="/api/agent-enrollment", tags=["agent-enrollment"])

_INTERACTION_COOKIE = "haku_agent_enrollment"
_INTERACTION_COOKIE_MAX_AGE_SECONDS = 10 * 60
_SETTINGS_ENROLLMENT_PATH = "/_console/settings/agents/enroll"


class AgentView(BaseModel):
    agent_id: UUID
    display_name: str
    status: AgentStatus
    credential_kind: CredentialKind
    credential_status: CredentialBindingStatus
    created_at: datetime.datetime
    activated_at: datetime.datetime | None
    last_seen_at: datetime.datetime | None
    access_profile_id: str | None


class AgentListResponse(BaseModel):
    agents: list[AgentView]
    access_profiles: list[str]


class UpdateAgentAccessProfileRequest(BaseModel):
    access_profile_id: str = Field(min_length=1)


class ReconnectableAgentView(BaseModel):
    agent_id: UUID
    display_name: str
    access_profile_id: str | None


class EnrollmentView(BaseModel):
    operator_display_name: str
    client_software: str
    redirect_host: str
    requested_scopes: list[str]
    suggested_agent_name: str
    reconnectable_agents: list[ReconnectableAgentView]
    access_profiles: list[str]
    default_access_profile_id: str
    form_token: str


class CreateEnrollmentRequest(BaseModel):
    kind: Literal["create"] = "create"
    form_token: str
    display_name: str
    access_profile_id: str = Field(min_length=1)


class ReconnectEnrollmentRequest(BaseModel):
    kind: Literal["reconnect"] = "reconnect"
    form_token: str
    agent_id: UUID
    access_profile_id: str = Field(min_length=1)


class DenyEnrollmentRequest(BaseModel):
    kind: Literal["deny"] = "deny"
    form_token: str


type EnrollmentDecisionRequest = Annotated[
    CreateEnrollmentRequest | ReconnectEnrollmentRequest | DenyEnrollmentRequest, Field(discriminator="kind")
]


class EnrollmentContinues(BaseModel):
    status: Literal["continue"] = "continue"
    authorization_url: str


class EnrollmentWasDenied(BaseModel):
    status: Literal["denied"] = "denied"


type EnrollmentDecisionResponse = EnrollmentContinues | EnrollmentWasDenied


OperatorSessionDep = Annotated[OperatorSession | None, Depends(operator_session)]


def _browser(session: OperatorSession) -> EnrollmentBrowserSession:
    return EnrollmentBrowserSession(
        operator_id=session.operator_id,
        identity_id=session.identity_id,
        browser_session_id=session.browser_session_id,
        display_name=session.username,
    )


def _login_redirect(interaction_id: UUID, browser_nonce: str | None) -> RedirectResponse:
    return_to = f"/auth/agent-enrollment/{interaction_id}"
    if browser_nonce is not None:
        return_to = f"{return_to}?{urlencode({'browser_nonce': browser_nonce})}"
    return RedirectResponse(url=f"/auth/login?{urlencode({'return_to': return_to})}", status_code=303)


def _raise_interaction_error(error: Exception) -> Never:
    if isinstance(error, EnrollmentInteractionNotFoundError):
        raise HTTPException(status_code=404, detail="Agent enrollment interaction not found") from error
    if isinstance(error, EnrollmentInteractionExpiredError):
        raise HTTPException(status_code=410, detail="Agent enrollment interaction expired") from error
    if isinstance(error, EnrollmentBrowserBindingError):
        raise HTTPException(status_code=403, detail="Agent enrollment browser binding is invalid") from error
    if isinstance(error, EnrollmentDecisionConflictError):
        raise HTTPException(
            status_code=409, detail="Agent enrollment decision conflicts with an earlier decision"
        ) from error
    raise AssertionError(f"unhandled Agent enrollment error: {type(error).__name__}")


def _cookie_path(interaction_id: UUID) -> str:
    return f"/api/agent-enrollment/{interaction_id}"


def _set_interaction_cookie(response: Response, interaction_id: UUID, page: EnrollmentPage, settings: Settings) -> None:
    response.set_cookie(
        key=_INTERACTION_COOKIE,
        value=page.form_token,
        max_age=_INTERACTION_COOKIE_MAX_AGE_SECONDS,
        path=_cookie_path(interaction_id),
        secure=settings.public_base_url.startswith("https://"),
        httponly=True,
        samesite="lax",
    )


def _require_operator(session: OperatorSession | None) -> OperatorSession:
    if session is None:
        raise HTTPException(status_code=401, detail="Operator authentication required")
    return session


async def _open_bound_interaction(
    *,
    interaction_id: UUID,
    browser_nonce: str | None,
    request: Request,
    service: AgentEnrollmentService,
    session: OperatorSession,
) -> EnrollmentPage:
    try:
        return await service.open_interaction(
            interaction_id=interaction_id,
            browser_nonce=browser_nonce,
            interaction_cookie=request.cookies.get(_INTERACTION_COOKIE),
            browser=_browser(session),
        )
    except (
        EnrollmentInteractionNotFoundError,
        EnrollmentInteractionExpiredError,
        EnrollmentBrowserBindingError,
        EnrollmentDecisionConflictError,
    ) as error:
        _raise_interaction_error(error)


def _enrollment_view(page: EnrollmentPage, session: OperatorSession) -> EnrollmentView:
    return EnrollmentView(
        operator_display_name=session.username,
        client_software=page.client_software,
        redirect_host=page.redirect_host,
        requested_scopes=list(page.requested_scopes),
        suggested_agent_name=page.suggested_agent_name,
        reconnectable_agents=[
            ReconnectableAgentView(
                agent_id=agent.agent_id, display_name=agent.display_name, access_profile_id=agent.access_profile_id
            )
            for agent in page.reconnectable_agents
        ],
        access_profiles=list(page.access_profiles),
        default_access_profile_id=page.default_access_profile_id,
        form_token=page.form_token,
    )


def _agent_view(agent: OperatorAgent) -> AgentView:
    return AgentView(
        agent_id=agent.agent_id,
        display_name=agent.display_name,
        status=agent.status,
        credential_kind=agent.credential_kind,
        credential_status=agent.credential_status,
        created_at=agent.created_at,
        activated_at=agent.activated_at,
        last_seen_at=agent.last_seen_at,
        access_profile_id=agent.access_profile_id,
    )


@entry_router.get("/{interaction_id}")
async def enrollment_entry(
    interaction_id: UUID,
    request: Request,
    service: AgentEnrollmentServiceDep,
    session: OperatorSessionDep,
    browser_nonce: str | None = None,
) -> Response:
    if session is None:
        return _login_redirect(interaction_id, browser_nonce)
    page = await _open_bound_interaction(
        interaction_id=interaction_id, browser_nonce=browser_nonce, request=request, service=service, session=session
    )
    response = RedirectResponse(url=f"{_SETTINGS_ENROLLMENT_PATH}/{interaction_id}", status_code=303)
    _set_interaction_cookie(response, interaction_id, page, cast(Settings, request.app.state.settings))
    return response


@operator_router.get("/agents", response_model=AgentListResponse)
async def list_agents(service: AgentEnrollmentServiceDep, session: OperatorSessionDep) -> AgentListResponse:
    operator = _require_operator(session)
    return AgentListResponse(
        agents=[_agent_view(agent) for agent in await service.list_agents(operator_id=operator.operator_id)],
        access_profiles=list(service.available_access_profiles()),
    )


@operator_router.put("/agents/{agent_id}/access-profile", response_model=AgentView)
async def update_agent_access_profile(
    agent_id: UUID,
    body: UpdateAgentAccessProfileRequest,
    service: AgentEnrollmentServiceDep,
    session: OperatorSessionDep,
) -> AgentView:
    operator = _require_operator(session)
    try:
        agent = await service.set_access_profile(
            operator_id=operator.operator_id, agent_id=agent_id, access_profile_id=body.access_profile_id
        )
    except AgentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Agent not found") from error
    except AgentAccessProfileManagedByDeploymentError as error:
        raise HTTPException(
            status_code=409, detail="Static Agent access profiles are managed by deployment configuration"
        ) from error
    except AccessProfileUnavailableError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _agent_view(agent)


@operator_router.get("/{interaction_id}", response_model=EnrollmentView)
async def get_enrollment(
    interaction_id: UUID, request: Request, service: AgentEnrollmentServiceDep, session: OperatorSessionDep
) -> EnrollmentView:
    operator = _require_operator(session)
    page = await _open_bound_interaction(
        interaction_id=interaction_id, browser_nonce=None, request=request, service=service, session=operator
    )
    return _enrollment_view(page, operator)


@operator_router.post("/{interaction_id}/decision", response_model=EnrollmentDecisionResponse)
async def decide_enrollment(
    interaction_id: UUID,
    body: EnrollmentDecisionRequest,
    request: Request,
    service: AgentEnrollmentServiceDep,
    session: OperatorSessionDep,
) -> Response:
    operator = _require_operator(session)
    interaction_cookie = request.cookies.get(_INTERACTION_COOKIE)
    if interaction_cookie is None:
        raise HTTPException(status_code=403, detail="missing Agent enrollment browser binding")

    decision: EnrollmentDecision
    match body:
        case CreateEnrollmentRequest(
            form_token=form_token, display_name=display_name, access_profile_id=access_profile_id
        ):
            decision = CreateAgentDecision(
                form_token=form_token, display_name=display_name, access_profile_id=access_profile_id
            )
        case ReconnectEnrollmentRequest(form_token=form_token, agent_id=agent_id, access_profile_id=access_profile_id):
            decision = ReconnectAgentDecision(
                form_token=form_token, agent_id=agent_id, access_profile_id=access_profile_id
            )
        case DenyEnrollmentRequest(form_token=form_token):
            decision = DenyEnrollmentDecision(form_token=form_token)

    try:
        result = await service.decide(
            interaction_id=interaction_id,
            browser=_browser(operator),
            interaction_cookie=interaction_cookie,
            decision=decision,
        )
    except AgentNameUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except InvalidAgentNameError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except AccessProfileUnavailableError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (
        EnrollmentInteractionNotFoundError,
        EnrollmentInteractionExpiredError,
        EnrollmentBrowserBindingError,
        EnrollmentDecisionConflictError,
    ) as error:
        _raise_interaction_error(error)

    response: Response
    match result:
        case EnrollmentAllowed(upstream_authorization_url=url):
            response = Response(
                content=EnrollmentContinues(authorization_url=url).model_dump_json(), media_type="application/json"
            )
        case EnrollmentDenied():
            response = Response(content=EnrollmentWasDenied().model_dump_json(), media_type="application/json")
    response.delete_cookie(key=_INTERACTION_COOKIE, path=_cookie_path(interaction_id))
    return response
