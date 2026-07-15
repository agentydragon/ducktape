from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.responses import HTMLResponse

AGENT_NAME_MAX_LENGTH = 80

_CSP_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    autoescape=select_autoescape(enabled_extensions=("html", "j2")),
    undefined=StrictUndefined,
)
_ENROLLMENT_TEMPLATE = _TEMPLATES.get_template("agent_enrollment.html.j2")
_CONTINUATION_TEMPLATE = _TEMPLATES.get_template("agent_enrollment_continuation.html.j2")


def http_origin(url: str) -> str:
    if any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise ValueError("URL contains whitespace or control characters")
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise ValueError("URL has an invalid origin") from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL must have an HTTP(S) origin without userinfo")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


@dataclass(frozen=True, slots=True)
class ReconnectAgentView:
    agent_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class AgentEnrollmentPageView:
    create_form_action: str
    reconnect_form_action: str
    deny_form_action: str
    form_token: str
    operator_display_name: str
    client_software: str
    redirect_host: str
    scopes: tuple[str, ...]
    suggested_agent_name: str
    reconnect_agents: tuple[ReconnectAgentView, ...]
    error: str | None = None


def render_agent_enrollment_page(
    view: AgentEnrollmentPageView, *, csp_nonce: str, form_action_url: str, status_code: int = 200
) -> HTMLResponse:
    if _CSP_NONCE_PATTERN.fullmatch(csp_nonce) is None:
        raise ValueError("CSP nonce must be a 32-128 character URL-safe base64 value")
    form_action_origin = http_origin(form_action_url)

    return HTMLResponse(
        status_code=status_code,
        content=_ENROLLMENT_TEMPLATE.render(
            view=view, csp_nonce=csp_nonce, agent_name_max_length=AGENT_NAME_MAX_LENGTH
        ),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                f"default-src 'none'; base-uri 'none'; form-action 'self' {form_action_origin}; "
                "frame-ancestors 'none'; "
                f"script-src 'none'; style-src 'nonce-{csp_nonce}'"
            ),
            "Referrer-Policy": "strict-origin",
            "Permissions-Policy": "geolocation=(), display-capture=()",
            "X-Content-Type-Options": "nosniff",
        },
    )


def render_agent_enrollment_continuation(*, authorization_url: str, csp_nonce: str) -> HTMLResponse:
    if _CSP_NONCE_PATTERN.fullmatch(csp_nonce) is None:
        raise ValueError("CSP nonce must be a 32-128 character URL-safe base64 value")
    http_origin(authorization_url)

    return HTMLResponse(
        content=_CONTINUATION_TEMPLATE.render(authorization_url=authorization_url, csp_nonce=csp_nonce),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
                f"script-src 'none'; style-src 'nonce-{csp_nonce}'"
            ),
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "geolocation=(), display-capture=()",
            "X-Content-Type-Options": "nosniff",
        },
    )
