"""The backend-served OAuth callback outcome page.

Shared by the console's OAuth connection flows: each redirects the browser straight to a
backend callback endpoint that runs the code→token exchange, then renders this minimal page to
report the outcome; only the title differs per flow. The markup lives in a sibling Jinja
template (loaded once at import) so it stays lintable rather than a Python string blob.

TODO: make this a SPA-style page instead of a backend-served .html template — have the
callback run the token exchange, then redirect to a frontend route that renders the outcome.
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
