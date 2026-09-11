"""Authenticated read API for a single Git snapshot index."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr

from haku.recall_index.embedder import Embedder
from x.agentplane.indexing.maintenance import Maintenance
from x.agentplane.indexing.store import Hit, Status, Store


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=12000)
    limit: int = Field(default=10, ge=1, le=100)


@dataclass(frozen=True)
class IndexStatus:
    index: Status
    source_error: str | None
    embedding_error: str | None
    gc_error: str | None


@dataclass(frozen=True)
class SearchResponse:
    hits: list[Hit]
    status: IndexStatus
    warning: str | None


def create_app(*, store: Store, embedder: Embedder, maintenance: Maintenance, read_token: SecretStr) -> FastAPI:
    async def authenticate(request: Request) -> None:
        headers = request.headers.getlist("authorization")
        expected = f"Bearer {read_token.get_secret_value()}".encode()
        if len(headers) != 1 or not secrets.compare_digest(headers[0].encode(), expected):
            raise HTTPException(401, "invalid bearer", headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(title="Agentplane Git index", docs_url=None, redoc_url=None, openapi_url=None)

    def index_status(state: Status) -> IndexStatus:
        return IndexStatus(
            index=state,
            source_error=maintenance.source_error,
            embedding_error=maintenance.embedding_error,
            gc_error=maintenance.gc_error,
        )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        await store.status()
        return {"status": "ok"}

    @app.get("/status", dependencies=[Depends(authenticate)])
    async def status() -> IndexStatus:
        return index_status(await store.status())

    @app.post("/search", dependencies=[Depends(authenticate)])
    async def search(body: SearchRequest) -> SearchResponse:
        vector = await embedder.embed_query(body.query)
        hits, snapshot_status = await store.search(vector, limit=body.limit)
        state = index_status(snapshot_status)
        warning = None
        if state.index.desired_digest is None:
            warning = "No source snapshot has been indexed yet."
        elif state.index.updating:
            warning = "Index update in progress; results may include older file revisions. Consult each hit's citation."
        if state.source_error is not None or state.embedding_error is not None:
            warning = "Index maintenance failed; results may be stale or incomplete. See status and service logs."
        return SearchResponse(hits=hits, status=state, warning=warning)

    return app
