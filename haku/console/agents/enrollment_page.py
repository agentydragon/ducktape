from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.responses import HTMLResponse

AGENT_NAME_MAX_LENGTH = 80

_CSP_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_TEMPLATE = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    autoescape=select_autoescape(enabled_extensions=("html", "j2")),
    undefined=StrictUndefined,
).get_template("agent_enrollment.html.j2")


@dataclass(frozen=True, slots=True)
class ReconnectAgentView:
    agent_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class AgentEnrollmentPageView:
    form_action: str
    operator_display_name: str
    client_software: str
    redirect_host: str
    scopes: tuple[str, ...]
    suggested_agent_name: str
    reconnect_agents: tuple[ReconnectAgentView, ...]
    error: str | None = None


def render_agent_enrollment_page(
    view: AgentEnrollmentPageView, *, csp_nonce: str, status_code: int = 200
) -> HTMLResponse:
    if _CSP_NONCE_PATTERN.fullmatch(csp_nonce) is None:
        raise ValueError("CSP nonce must be a 32-128 character URL-safe base64 value")

    return HTMLResponse(
        status_code=status_code,
        content=_TEMPLATE.render(view=view, csp_nonce=csp_nonce, agent_name_max_length=AGENT_NAME_MAX_LENGTH),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
                f"script-src 'none'; style-src 'nonce-{csp_nonce}'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
