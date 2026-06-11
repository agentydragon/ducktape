#!/usr/bin/env python3
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus

import httpx
import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from structlog.stdlib import BoundLogger

from .policy_common import (
    API_TIMEOUT,
    CACHE,
    EXEMPT_USERS,
    GITEA_BASE,
    get_limit_for_repo,
    normalize_login,
    parse_owner_repo_from_uri,
    parse_reopen_targets,
    user_headers,
)


@dataclass
class AppResources:
    http: httpx.AsyncClient
    log: BoundLogger


@asynccontextmanager
async def lifespan(app: FastAPI):
    http_client = httpx.AsyncClient()
    logging.basicConfig(level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    log = structlog.get_logger().bind(app="pr_quota")
    app.state.resources = AppResources(http=http_client, log=log)
    try:
        yield
    finally:
        await http_client.aclose()


app = FastAPI(title="Gitea PR Quota Policy", version="1.0.0", lifespan=lifespan)
TRUST_PROXY_USER = os.environ.get("PRQ_TRUST_PROXY_USER", "false").lower() in {"1", "true", "yes", "on"}


# Prometheus metrics
REQUESTS = Counter("pr_quota_requests_total", "Total PR quota checks", labelnames=("action", "outcome"))
DURATION = Histogram("pr_quota_request_seconds", "PR quota check latency in seconds", labelnames=("action", "outcome"))

# cache provided by policy_common.CACHE


def get_resources(request: Request) -> AppResources:
    resources = getattr(request.app.state, "resources", None)
    if not isinstance(resources, AppResources):
        raise RuntimeError("App resources not initialized")
    return resources


async def get_doer_login(client: httpx.AsyncClient, cookie: str, authz: str) -> str:
    try:
        resp = await client.get(f"{GITEA_BASE}api/v1/user", headers=user_headers(cookie, authz), timeout=API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        login = data.get("login", "")
        return str(login) if login is not None else ""
    except httpx.HTTPStatusError as e:
        # Authentication/authorization failure
        raise HTTPException(status_code=HTTPStatus.UNAUTHORIZED, detail=f"auth error: {e.response.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail=f"upstream error: {e}")


async def count_open_prs_by_author(client: httpx.AsyncClient, owner: str, repo: str, author_login: str) -> int:
    # normalize + cache
    author_login = normalize_login(author_login)
    if (cached := CACHE.get(owner, repo, author_login)) is not None:
        return cached
    headers = user_headers(admin=True)
    try:
        # Prefer Issues API with filter + X-Total-Count
        resp = await client.get(
            f"{GITEA_BASE}api/v1/repos/{owner}/{repo}/issues",
            params={"state": "open", "type": "pulls", "created_by": author_login, "page": 1, "limit": 1},
            headers=headers,
            timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        total = resp.headers.get("X-Total-Count") or resp.headers.get("x-total-count")
        count = int(total) if total is not None else len(resp.json())
        CACHE.set(owner, repo, author_login, count)
        return count
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=f"count error: {e.response.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail=f"upstream error: {e}")


async def enforce_quota(client: httpx.AsyncClient, owner: str, repo: str, doer: str) -> tuple[HTTPStatus, str]:
    if not doer:
        return HTTPStatus.UNAUTHORIZED, "no user"
    doer_l = doer.lower()
    if doer_l in EXEMPT_USERS:
        return HTTPStatus.NO_CONTENT, "allow (exempt)"
    if (limit := get_limit_for_repo(owner, repo)) <= 0:
        return HTTPStatus.NO_CONTENT, "allow (unlimited)"
    if (open_count := await count_open_prs_by_author(client, owner, repo, doer_l)) >= limit:
        return HTTPStatus.FORBIDDEN, f"PR quota exceeded: {open_count}/{limit}"
    return HTTPStatus.NO_CONTENT, "allow"


def log_decision(
    log: BoundLogger,
    *,
    action: str,
    owner: str,
    repo: str,
    user: str,
    status: HTTPStatus,
    reason: str,
    started_at: float,
    pr_index: int | None = None,
) -> None:
    outcome = "allow" if status == HTTPStatus.NO_CONTENT else "deny"
    DURATION.labels(action, outcome).observe(max(time.monotonic() - started_at, 0))
    REQUESTS.labels(action, outcome).inc()
    bound = log.bind(action=action, owner=owner, repo=repo, user=user)
    bound.info("decision", status=int(status), reason=reason, pr=pr_index)


@app.get("/validate")
async def validate(request: Request, resources: AppResources = Depends(get_resources)) -> Response:  # noqa: B008
    # Headers forwarded by nginx internal location
    orig_uri = request.headers.get("X-Original-URI", "")
    cookie = request.headers.get("Cookie", "")
    authz = request.headers.get("Authorization", "")

    # Optional: trust reverse-proxy user header for identity (if enabled)
    proxy_user = request.headers.get("X-Original-User") if TRUST_PROXY_USER else None

    client = resources.http
    log = resources.log
    # 1) PR creation endpoints
    if owner_repo := parse_owner_repo_from_uri(orig_uri):
        owner, repo = owner_repo
        doer = proxy_user or await get_doer_login(client, cookie, authz)
        started = time.monotonic()
        status, msg = await enforce_quota(client, owner, repo, doer)
        log_decision(
            log, action="create", owner=owner, repo=repo, user=doer, status=status, reason=msg, started_at=started
        )
        return Response(status_code=status, content=("" if status == HTTPStatus.NO_CONTENT else msg))

    # 2) Reopen/close endpoints — if currently closed, treat as reopen
    if ro := parse_reopen_targets(orig_uri):
        owner, repo, pr_index = ro
        try:
            pr_resp = await client.get(
                f"{GITEA_BASE}api/v1/repos/{owner}/{repo}/pulls/{pr_index}",
                headers=user_headers(admin=True),
                timeout=API_TIMEOUT,
            )
            pr_resp.raise_for_status()
            pr = pr_resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=f"get pr error: {e.response.status_code}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail=f"upstream error: {e}")

        if (pr.get("state") or "").lower() != "closed":
            return Response(status_code=HTTPStatus.NO_CONTENT)

        doer = proxy_user or await get_doer_login(client, cookie, authz)
        started = time.monotonic()
        status, msg = await enforce_quota(client, owner, repo, doer)
        log_decision(
            log,
            action="reopen",
            owner=owner,
            repo=repo,
            user=doer,
            status=status,
            reason=msg,
            started_at=started,
            pr_index=pr_index,
        )
        return Response(status_code=status, content=("" if status == HTTPStatus.NO_CONTENT else msg))

    # Not a path we validate -> allow
    return Response(status_code=HTTPStatus.NO_CONTENT)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# To run:
#   uvicorn gitea_pr_gate.policy_server_fastapi:app --host 127.0.0.1 --port 9099


# Optional: global exception handlers for catch-all upstream errors
@app.exception_handler(httpx.HTTPError)
async def handle_httpx_error(_: Request, exc: httpx.HTTPError):
    return Response(status_code=HTTPStatus.SERVICE_UNAVAILABLE, content=f"upstream error: {exc}")


@app.exception_handler(Exception)
async def handle_uncaught(_: Request, exc: Exception):
    return Response(status_code=HTTPStatus.INTERNAL_SERVER_ERROR, content="policy error")
