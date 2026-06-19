"""Load the dashboard page template + CSS, preferring overrides committed to the
haku-state clone (``dashboard/templates/``) so Haku can evolve the look without an
image rebuild, and **failing safe** to the baked defaults if an override is
missing, unreadable, or broken — a bad state commit can never take the dashboard
down.
"""

from __future__ import annotations

import logging
from pathlib import Path

import jinja2

logger = logging.getLogger(__name__)

_BAKED = Path(__file__).resolve().parent / "templates"
_ENV = jinja2.Environment(autoescape=True, undefined=jinja2.StrictUndefined)

# Compiled once: the common path (no override committed) is zero-IO.
_BAKED_TEMPLATE = _ENV.from_string((_BAKED / "page.html.j2").read_text())
_BAKED_CSS = (_BAKED / "style.css").read_text()

# Dummy context to smoke-test an override template before trusting it.
_SMOKE_CTX = {
    "css": "",
    "intake_new": "",
    "up_next_html": "",
    "backlog_html": "",
    "open_count": 0,
    "counts": "",
    "scan_time": "",
}


def _override(clone_dir: Path, name: str) -> Path:
    return clone_dir / "dashboard" / "templates" / name


def load_css(clone_dir: Path) -> str:
    override = _override(clone_dir, "style.css")
    if override.is_file():
        try:
            return override.read_text()
        except OSError:
            logger.warning("override style.css unreadable; using baked default", exc_info=True)
    return _BAKED_CSS


def load_page_template(clone_dir: Path) -> jinja2.Template:
    override = _override(clone_dir, "page.html.j2")
    if override.is_file():
        try:
            template = _ENV.from_string(override.read_text())
            template.render(**_SMOKE_CTX)  # raises on syntax/undefined errors
            return template
        except (OSError, jinja2.TemplateError):
            logger.warning("override page.html.j2 invalid; using baked default", exc_info=True)
    return _BAKED_TEMPLATE
