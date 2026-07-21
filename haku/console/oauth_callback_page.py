"""Backend fallback for operator-login callback failures.

Account-link callbacks hand their results to the SPA; operator login cannot assume that the SPA
has a working authenticated session, so its failure and retry page remains backend-rendered. See
``docs/oauth_browser_surfaces.md`` for the ownership boundary and consolidation plan.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

_CALLBACK_TEMPLATE = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    autoescape=select_autoescape(enabled_extensions=("html", "j2")),
    undefined=StrictUndefined,
).get_template("oauth_callback.html.j2")


def render_oauth_callback_page(
    title: str, message: str, *, status_code: int = 200, action_url: str | None = None, action_label: str | None = None
) -> HTMLResponse:
    csp_nonce = secrets.token_urlsafe(32)
    return HTMLResponse(
        status_code=status_code,
        content=_CALLBACK_TEMPLATE.render(
            title=title, message=message, csp_nonce=csp_nonce, action_url=action_url, action_label=action_label
        ),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
                f"script-src 'none'; style-src 'nonce-{csp_nonce}'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
