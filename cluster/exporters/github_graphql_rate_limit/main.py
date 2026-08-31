"""Prometheus exporter for GitHub's actual GraphQL rate-limit bucket."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Response

logger = logging.getLogger(__name__)

_ACCOUNT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_GRAPHQL_QUERY = "query { rateLimit { cost } }"
_REQUIRED_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-used", "x-ratelimit-reset")


@dataclass(frozen=True)
class Settings:
    account: str
    token: str
    github_url: str = "https://api.github.com/graphql"

    @classmethod
    def from_env(cls) -> Settings:
        account = os.environ["GITHUB_ACCOUNT"]
        if _ACCOUNT_RE.fullmatch(account) is None:
            raise ValueError(f"invalid GitHub account name: {account!r}")
        token = Path(os.environ["GITHUB_TOKEN_FILE"]).read_text().strip()
        if not token:
            raise ValueError("GitHub token file is empty")
        return cls(
            account=account,
            token=token,
            github_url=os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql"),
        )


@dataclass(frozen=True)
class RateLimit:
    limit: int
    remaining: int
    used: int
    reset: int


async def query_rate_limit(client: httpx.AsyncClient, settings: Settings) -> RateLimit:
    response = await client.post(
        settings.github_url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {settings.token}",
            "User-Agent": "ducktape-github-graphql-rate-limit-exporter",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"query": _GRAPHQL_QUERY},
    )
    response.raise_for_status()
    if response.headers.get("x-ratelimit-resource") != "graphql":
        raise ValueError("GitHub response did not identify the GraphQL rate-limit resource")
    missing = [name for name in _REQUIRED_HEADERS if name not in response.headers]
    if missing:
        raise ValueError(f"GitHub response omitted rate-limit headers: {', '.join(missing)}")
    return RateLimit(
        limit=int(response.headers["x-ratelimit-limit"]),
        remaining=int(response.headers["x-ratelimit-remaining"]),
        used=int(response.headers["x-ratelimit-used"]),
        reset=int(response.headers["x-ratelimit-reset"]),
    )


def render_metrics(account: str, rate_limit: RateLimit) -> str:
    label = f'github_account="{account}"'
    values = (
        ("github_graphql_rate_limit", "GitHub GraphQL primary rate-limit points.", rate_limit.limit),
        ("github_graphql_rate_remaining", "GitHub GraphQL primary rate-limit points remaining.", rate_limit.remaining),
        ("github_graphql_rate_used", "GitHub GraphQL primary rate-limit points used.", rate_limit.used),
        (
            "github_graphql_rate_reset_timestamp_seconds",
            "Unix timestamp when the GitHub GraphQL primary rate limit resets.",
            rate_limit.reset,
        ),
    )
    lines: list[str] = []
    for name, help_text, value in values:
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name}{{{label}}} {value}"))
    return "\n".join((*lines, "# EOF", ""))


def create_app(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            app.state.github_client = client
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        try:
            rate_limit = await query_rate_limit(app.state.github_client, settings)
        except (httpx.HTTPError, ValueError) as exc:
            logger.exception("GitHub GraphQL quota probe failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(
            render_metrics(settings.account, rate_limit), media_type="application/openmetrics-text; version=1.0.0"
        )

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(Settings.from_env()), host="0.0.0.0", port=9172)


if __name__ == "__main__":
    main()
