"""FastAPI routes for Haku's Operator-authenticated Agent enrollment page."""

from __future__ import annotations

import secrets
from typing import Annotated, Never, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import FormData
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from haku.console.agents.enrollment import (
    AgentEnrollmentService,
    AgentNameUnavailableError,
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
    ReconnectAgentDecision,
)
from haku.console.agents.enrollment_page import (
    AgentEnrollmentPageView,
    ReconnectAgentView,
    http_origin,
    render_agent_enrollment_page,
)
from haku.console.agents.naming import InvalidAgentNameError
from haku.console.config import Settings
from haku.console.operator_auth import OperatorSession, operator_session

router = APIRouter(prefix="/auth/agent-enrollment", tags=["agent-enrollment"])

_INTERACTION_COOKIE = "haku_agent_enrollment"
_INTERACTION_COOKIE_MAX_AGE_SECONDS = 10 * 60


def _enrollment_service(request: Request) -> AgentEnrollmentService:
    return cast(AgentEnrollmentService, request.app.state.agent_enrollment_service)


def _operator_session(request: Request) -> OperatorSession | None:
    return operator_session(request)


EnrollmentServiceDep = Annotated[AgentEnrollmentService, Depends(_enrollment_service)]
OperatorSessionDep = Annotated[OperatorSession | None, Depends(_operator_session)]


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


def _public_origin(settings: Settings) -> str:
    return http_origin(settings.public_base_url)


def _require_same_origin(request: Request) -> None:
    settings = cast(Settings, request.app.state.settings)
    if request.headers.get("origin") != _public_origin(settings):
        raise HTTPException(status_code=403, detail="invalid Agent enrollment origin")


def _one_form_value(form: FormData, name: str) -> str:
    values = form.getlist(name)
    if len(values) != 1 or not isinstance(values[0], str):
        raise HTTPException(status_code=400, detail=f"invalid {name}")
    return values[0]


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
    return f"/auth/agent-enrollment/{interaction_id}"


def _render_page(
    *,
    interaction_id: UUID,
    session: OperatorSession,
    page: EnrollmentPage,
    settings: Settings,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    base = _cookie_path(interaction_id)
    response = render_agent_enrollment_page(
        AgentEnrollmentPageView(
            create_form_action=f"{base}/new",
            reconnect_form_action=f"{base}/reconnect",
            deny_form_action=f"{base}/deny",
            form_token=page.form_token,
            operator_display_name=session.username,
            client_software=page.client_software,
            redirect_host=page.redirect_host,
            scopes=page.requested_scopes,
            suggested_agent_name=page.suggested_agent_name,
            reconnect_agents=tuple(
                ReconnectAgentView(agent_id=str(agent.agent_id), display_name=agent.display_name)
                for agent in page.reconnectable_agents
            ),
            error=error,
        ),
        csp_nonce=secrets.token_urlsafe(32),
        form_action_url=page.upstream_authorization_url,
        status_code=status_code,
    )
    response.set_cookie(
        key=_INTERACTION_COOKIE,
        value=page.form_token,
        max_age=_INTERACTION_COOKIE_MAX_AGE_SECONDS,
        path=base,
        secure=settings.public_base_url.startswith("https://"),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/{interaction_id}")
async def enrollment_page(
    interaction_id: UUID,
    request: Request,
    service: EnrollmentServiceDep,
    session: OperatorSessionDep,
    browser_nonce: str | None = None,
) -> Response:
    if session is None:
        return _login_redirect(interaction_id, browser_nonce)
    try:
        page = await service.open_interaction(
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
    return _render_page(
        interaction_id=interaction_id, session=session, page=page, settings=cast(Settings, request.app.state.settings)
    )


async def _decide(
    *,
    interaction_id: UUID,
    request: Request,
    service: AgentEnrollmentService,
    session: OperatorSession,
    decision: EnrollmentDecision,
) -> Response:
    interaction_cookie = request.cookies.get(_INTERACTION_COOKIE)
    if interaction_cookie is None:
        raise HTTPException(status_code=403, detail="missing Agent enrollment browser binding")
    try:
        result = await service.decide(
            interaction_id=interaction_id,
            browser=_browser(session),
            interaction_cookie=interaction_cookie,
            decision=decision,
        )
    except (AgentNameUnavailableError, InvalidAgentNameError) as error:
        try:
            page = await service.open_interaction(
                interaction_id=interaction_id,
                browser_nonce=None,
                interaction_cookie=interaction_cookie,
                browser=_browser(session),
            )
        except (
            EnrollmentInteractionNotFoundError,
            EnrollmentInteractionExpiredError,
            EnrollmentBrowserBindingError,
            EnrollmentDecisionConflictError,
        ) as reopen_error:
            _raise_interaction_error(reopen_error)
        return _render_page(
            interaction_id=interaction_id,
            session=session,
            page=page,
            settings=cast(Settings, request.app.state.settings),
            error=str(error),
            status_code=409 if isinstance(error, AgentNameUnavailableError) else 422,
        )
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
            response = RedirectResponse(url=url, status_code=303)
        case EnrollmentDenied():
            response = PlainTextResponse("Agent enrollment denied. You may close this window.")
    response.delete_cookie(key=_INTERACTION_COOKIE, path=_cookie_path(interaction_id))
    return response


def _require_authenticated_post(request: Request, session: OperatorSession | None) -> OperatorSession:
    if session is None:
        raise HTTPException(status_code=401, detail="Operator authentication required")
    _require_same_origin(request)
    return session


@router.post("/{interaction_id}/new")
async def create_agent(
    interaction_id: UUID, request: Request, service: EnrollmentServiceDep, session: OperatorSessionDep
) -> Response:
    session = _require_authenticated_post(request, session)
    form = await request.form()
    return await _decide(
        interaction_id=interaction_id,
        request=request,
        service=service,
        session=session,
        decision=CreateAgentDecision(
            form_token=_one_form_value(form, "form_token"), display_name=_one_form_value(form, "agent_name")
        ),
    )


@router.post("/{interaction_id}/reconnect")
async def reconnect_agent(
    interaction_id: UUID, request: Request, service: EnrollmentServiceDep, session: OperatorSessionDep
) -> Response:
    session = _require_authenticated_post(request, session)
    form = await request.form()
    try:
        agent_id = UUID(_one_form_value(form, "agent_id"))
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid reconnect Agent") from None
    return await _decide(
        interaction_id=interaction_id,
        request=request,
        service=service,
        session=session,
        decision=ReconnectAgentDecision(form_token=_one_form_value(form, "form_token"), agent_id=agent_id),
    )


@router.post("/{interaction_id}/deny")
async def deny_agent(
    interaction_id: UUID, request: Request, service: EnrollmentServiceDep, session: OperatorSessionDep
) -> Response:
    session = _require_authenticated_post(request, session)
    form = await request.form()
    return await _decide(
        interaction_id=interaction_id,
        request=request,
        service=service,
        session=session,
        decision=DenyEnrollmentDecision(form_token=_one_form_value(form, "form_token")),
    )
