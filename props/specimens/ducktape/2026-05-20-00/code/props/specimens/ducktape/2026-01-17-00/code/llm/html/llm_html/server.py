#!/usr/bin/env python3
"""FastAPI server for LLM instructions with token generation."""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import markdown
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from jinja2 import Environment, FileSystemLoader
from markdownify import markdownify
from pydantic import BaseModel, ConfigDict

from .token_counter import count_tokens_for_models
from .token_scheme import TokenScheme, VerificationError


class StatsCache(BaseModel):
    """Cache for page statistics with TTL."""

    model_config = ConfigDict(validate_assignment=True, arbitrary_types_allowed=True)

    data: dict | None = None
    updated_at: datetime | None = None
    ttl: timedelta


# Configure logging to output to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Global configuration
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "hunter2").encode()

# List of markdown pages to serve (without .md extension)
MARKDOWN_PAGES = ["tana", "coding"]

# Cache for page titles from frontmatter
PAGE_TITLES = {}

# Cache for stats with TTL
STATS_CACHE = StatsCache(ttl=timedelta(minutes=5))

# Common security headers for all responses
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self' https://cdn.jsdelivr.net data:; style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; img-src 'self' data:",
}

# Site configuration
SITE_URL = os.environ.get("SITE_URL", "http://llm.agentydragon.com")
TIMEZONE = ZoneInfo("America/Los_Angeles")

# FastAPI setup
app = FastAPI(title="LLM Instructions Server")

# Jinja2 setup
env = Environment(loader=FileSystemLoader("."), trim_blocks=True, lstrip_blocks=True)


def create_markdown_converter() -> markdown.Markdown:
    """Create a configured markdown converter instance."""
    return markdown.Markdown(extensions=["tables", "fenced_code", "meta"])


def load_page_titles():
    """Load titles from frontmatter of all markdown pages."""
    for page in MARKDOWN_PAGES:
        try:
            text = Path(f"{page}.md").read_text()
            md = create_markdown_converter()
            md.convert(text)
            if hasattr(md, "Meta") and "title" in md.Meta:
                PAGE_TITLES[page] = md.Meta["title"][0]
            else:
                raise ValueError(f"Missing required 'title' in frontmatter for {page}.md")
        except Exception as e:
            logger.error(f"Error loading title for {page}.md: {e}")
            raise


# Load page titles at startup
load_page_titles()


def handle_page_rendering_error(error: Exception, page_name: str = "page") -> None:
    """Handle common page rendering errors by logging and raising appropriate HTTPException.

    Args:
        error: The exception that occurred
        page_name: Name of the page for logging context

    Raises:
        HTTPException: Always raises with appropriate status code
    """
    if isinstance(error, FileNotFoundError):
        logger.error(f"{page_name} not found")
        raise HTTPException(status_code=404, detail="Document not found")
    logger.error(f"Error rendering {page_name}: {error}")
    raise HTTPException(status_code=500, detail="Internal server error")


def render_html_page(title: str, content: str, active_page: str = "index") -> str:
    """Render HTML page with common structure and navigation menu."""
    template = env.get_template("base.html")
    return template.render(
        title=title, content=content, active_page=active_page, markdown_pages=MARKDOWN_PAGES, page_titles=PAGE_TITLES
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main page with rendered markdown."""
    try:
        text = await asyncio.to_thread(Path("index.md").read_text)
        ts = TokenScheme(TOKEN_SECRET, text)

        # Use configured timezone
        current_time = datetime.now(TIMEZONE)
        prefix, bits = ts.make_token(current_time)

        # Render template
        tpl = env.get_template("index.md")
        text = tpl.render(prefix=prefix, bits=bits, site_url=SITE_URL)

        # Convert to HTML
        md = create_markdown_converter()
        content = md.convert(text)

        # Render with menu
        html = render_html_page("LLM Instructions", content, active_page="index")

        return HTMLResponse(content=html, headers=HEADERS)
    except Exception as e:
        handle_page_rendering_error(e, "index.md")


# Create routes for each markdown page
for page_name in MARKDOWN_PAGES:

    @app.get(f"/{page_name}", response_class=HTMLResponse, name=page_name)
    async def serve_markdown_page(page: str = page_name):
        """Serve a markdown documentation page."""
        try:
            text = await asyncio.to_thread(Path(f"{page}.md").read_text)

            # Convert to HTML with frontmatter support
            md = create_markdown_converter()
            content = md.convert(text)

            # Get title from frontmatter (required)
            if not hasattr(md, "Meta") or "title" not in md.Meta:
                raise ValueError(f"Missing required 'title' in frontmatter for {page}.md")
            title = md.Meta["title"][0]

            # Render with menu
            html = render_html_page(title, content, active_page=page)

            return HTMLResponse(content=html, headers=HEADERS)
        except Exception as e:
            handle_page_rendering_error(e, f"{page}.md")


async def analyze_page_tokens(
    page_id: str, markdown_path: Path, title: str, url: str, is_index: bool = False
) -> dict[str, Any] | None:
    """Analyze a single page's token counts by simulating the full rendering pipeline."""
    try:
        # Step 1: Read markdown
        text = await asyncio.to_thread(markdown_path.read_text)

        if is_index:
            # Step 2: Render template variables for index
            ts = TokenScheme(TOKEN_SECRET, text)
            current_time = datetime.now(TIMEZONE)
            prefix, bits = ts.make_token(current_time)
            tpl = env.get_template("index.md")
            rendered_markdown = tpl.render(prefix=prefix, bits=bits, site_url=SITE_URL)
            md = create_markdown_converter()
            html_content = md.convert(rendered_markdown)
        else:
            # Step 2: Convert to HTML with frontmatter
            md_converter = create_markdown_converter()
            html_content = md_converter.convert(text)

        # Step 3: Render full HTML page with navigation
        full_html = render_html_page(title, html_content, active_page=page_id)

        # Step 4: Convert full HTML (including nav) back to markdown
        final_markdown = markdownify(full_html, heading_style="ATX")

        # Step 5: Count tokens on the final markdown
        tokens = count_tokens_for_models(final_markdown)
        return {"page": page_id, "title": title, "url": url, **tokens}
    except Exception as e:
        logger.error(f"Error analyzing {page_id} page: {e}")
        return None


@app.get("/api/stats")
async def stats_api():
    """Return statistics about all served pages as JSON."""
    # Check cache
    now = datetime.now(TIMEZONE)
    if (
        STATS_CACHE.data is not None
        and STATS_CACHE.updated_at is not None
        and now - STATS_CACHE.updated_at < STATS_CACHE.ttl
    ):
        logger.info("Returning cached stats")
        return STATS_CACHE.data

    logger.info("Calculating fresh stats")
    pages_stats = []

    # Analyze index page
    if stats := await analyze_page_tokens("index", Path("index.md"), "LLM Instructions", "/", is_index=True):
        pages_stats.append(stats)

    # Analyze other markdown pages
    for page in MARKDOWN_PAGES:
        title = PAGE_TITLES.get(page, page)
        if stats := await analyze_page_tokens(page, Path(f"{page}.md"), title, f"/{page}"):
            pages_stats.append(stats)

    # Calculate totals
    totals = {
        "claude-4": sum(stat["claude-4"] for stat in pages_stats),
        "o3": sum(stat["o3"] for stat in pages_stats),
        "bytes": sum(stat["bytes"] for stat in pages_stats),
    }

    result = {"pages": pages_stats, "totals": totals}

    # Update cache
    STATS_CACHE.data = result
    STATS_CACHE.updated_at = now

    return result


@app.get("/stats", response_class=HTMLResponse)
async def stats_page():
    """Show statistics page (loads data via API)."""
    # Render the stats template
    template = env.get_template("stats.html")
    html = template.render(
        title="Server Statistics", active_page="stats", markdown_pages=MARKDOWN_PAGES, page_titles=PAGE_TITLES
    )
    return HTMLResponse(content=html, headers=HEADERS)


@app.get("/style.css")
async def style_css():
    """Serve the CSS file."""
    file_path = Path("style.css")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="style.css not found")

    return FileResponse(file_path, media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/verify/{token:path}", response_class=HTMLResponse)
@app.get("/verify", response_class=HTMLResponse)
async def verify_token(request: Request, token: str = ""):
    """Verify a token against the current document."""
    # Check if token is in query params (for form submission)
    if not token and "token" in request.query_params:
        token = request.query_params["token"]

    result: dict[str, Any] | None = None
    if token:
        try:
            # Read the source index.md file (not rendered)
            text = await asyncio.to_thread(Path("index.md").read_text)
            ts = TokenScheme(TOKEN_SECRET, text)

            ts.verify_token(token)
            result = {"status": "success", "message": "Token is valid ✅"}
            logger.info(f"Token verification succeeded for: {token[:20]}...")
        except VerificationError as exc:
            result = {"status": "failed", "errors": exc.issues}
            issues_str = " | ".join(f"✗ {issue}" for issue in exc.issues)
            logger.error(f"Token verification FAILED: {issues_str}")
        except FileNotFoundError:
            logger.error("index.md not found for token verification")
            result = {"status": "failed", "errors": ["Source document not found"]}
        except Exception as e:
            logger.error(f"Unexpected error during token verification: {e}")
            result = {"status": "failed", "errors": ["Internal error during verification"]}

    # Render the verification page
    template = env.get_template("verify.html")
    html = template.render(token=token, result=result, markdown_pages=MARKDOWN_PAGES, site_url=SITE_URL)
    return HTMLResponse(content=html, headers=HEADERS)


def main():
    """Main entry point for the server."""
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "9000"))

    logger.info(f"Starting FastAPI server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_config=None)  # None to use our logging config


if __name__ == "__main__":
    main()
