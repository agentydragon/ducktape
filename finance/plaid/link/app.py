"""Plaid v0 web app: Link UI plus synchronous full-refresh sync."""

from __future__ import annotations

import contextlib
import logging
import re
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from importlib import resources
from typing import Annotated, Literal, Protocol
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Path as ApiPath, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from finance.plaid.db.client import (
    InstitutionDetail,
    InstitutionSummary,
    LinkTokenResult,
    PlaidClient,
    PlaidClientError,
    PlaidCreds,
    PublicTokenExchange,
)
from finance.plaid.db.config import MAX_TRANSACTION_DAYS, PlaidWebSettings
from finance.plaid.db.link_store import PlaidLinkStorage, StoredLink
from finance.plaid.db.products import Product, syncable_products
from finance.plaid.db.secret_store import K8sSecretStore, SecretStore
from finance.plaid.db.sync import PlaidApiLike, sync_link

logger = logging.getLogger(__name__)

# The Link UI is HTML, CSS and JS, and gets prettier, editor syntax highlighting and diffs that
# mean something only while it lives in files with those extensions. Read once at import; these
# are static assets, not templates.
_ASSETS = resources.files(__package__)
_LINK_HTML = _ASSETS.joinpath("link.html").read_text("utf-8")
_LINK_CSS = _ASSETS.joinpath("link.css").read_text("utf-8")
_LINK_JS = _ASSETS.joinpath("link.js").read_text("utf-8")


class LinkTokenRequest(BaseModel):
    institution_id: str
    products: list[Product] = Field(min_length=1)
    transaction_days_requested: int | None = Field(default=None, ge=1, le=MAX_TRANSACTION_DAYS)


class LinkTokenResponse(BaseModel):
    link_token: str
    products: list[str]
    transaction_days_requested: int | None


class WebConfigResponse(BaseModel):
    transaction_days: int
    max_transaction_days: int = MAX_TRANSACTION_DAYS


class InstitutionSearchResult(BaseModel):
    institution_id: str
    name: str


class InstitutionProductsResponse(BaseModel):
    institution_id: str
    name: str
    url: str | None
    # What the institution offers and this app can mirror; preselected in the UI.
    syncable_products: list[Product]
    # Offered but not mirrored here (auth, identity, ...). Shown so the list reads as a deliberate
    # narrowing rather than a gap.
    unsupported_products: list[str]


class LinkUpdateTokenRequest(BaseModel):
    reason: Literal["repair", "add_scope"] = "repair"
    products: list[Product] | None = None


class LinkUpdateTokenResponse(BaseModel):
    link_token: str
    products: list[str]
    additional_products: list[str]


class CompleteLinkUpdateRequest(BaseModel):
    products: list[str] = Field(default_factory=list)
    sync: bool = True


class SyncResponse(BaseModel):
    run_id: str


class ExchangePublicTokenRequest(BaseModel):
    public_token: str
    products: list[str] = Field(min_length=1)
    transaction_days_requested: int | None = Field(default=None, ge=1, le=MAX_TRANSACTION_DAYS)
    label: str | None = None
    institution_id: str | None = None
    institution_name: str | None = None


class LinkSummary(BaseModel):
    item_id: str
    label: str | None
    institution_id: str | None
    institution_name: str | None
    products_requested: list[str]
    transaction_days_requested: int | None
    earliest_transaction_date: str | None
    latest_transaction_date: str | None
    observed_transaction_history_days: int | None
    synced_transaction_count: int
    products_authorized: list[str]
    products_billed: list[str]
    status: str
    access_token_secret: str
    last_synced_at: str | None


class PlaidWebClient(PlaidApiLike, Protocol):
    """Plaid operations used by the Link management UI and sync path."""

    def close(self) -> None: ...
    def create_link_token(
        self,
        *,
        products: list[str],
        redirect_uri: str,
        client_user_id: str,
        institution_id: str | None = None,
        transaction_days_requested: int = 730,
        client_name: str = "Plaid MCP",
    ) -> LinkTokenResult: ...
    def search_institutions(self, query: str, *, count: int = 10) -> list[InstitutionSummary]: ...
    def get_institution(self, institution_id: str) -> InstitutionDetail: ...
    def create_update_link_token(
        self,
        *,
        access_token: str,
        redirect_uri: str,
        client_user_id: str,
        additional_products: list[str] | None = None,
        client_name: str = "Plaid MCP",
    ) -> LinkTokenResult: ...
    def exchange_public_token(self, public_token: str) -> PublicTokenExchange: ...
    def remove_item(self, access_token: str) -> None: ...


class AppState:
    def __init__(self) -> None:
        self.client: PlaidWebClient | None = None
        self.storage: PlaidLinkStorage | None = None
        self.secrets: SecretStore | None = None


def create_app(
    settings: PlaidWebSettings,
    *,
    storage: PlaidLinkStorage | None = None,
    secrets: SecretStore | None = None,
    client: PlaidWebClient | None = None,
) -> FastAPI:
    state = AppState()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_client = None
        owned_secrets = None
        if client is None:
            owned_client = PlaidClient(
                PlaidCreds(client_id=settings.client_id, secret=settings.client_secret, env=settings.plaid_env)
            )
            state.client = owned_client
        else:
            state.client = client
        state.storage = storage or await PlaidLinkStorage.initialize(settings.database_url)
        if secrets is None:
            owned_secrets = await K8sSecretStore.from_incluster(settings.namespace, settings.managed_by)
            state.secrets = owned_secrets
        else:
            state.secrets = secrets
        try:
            yield
        finally:
            state.client = None
            if owned_secrets is not None:
                await owned_secrets.close()
            if storage is None and state.storage is not None:
                await state.storage.close()
            if owned_client is not None:
                owned_client.close()

    app = FastAPI(title="Plaid Link Service", docs_url=None, redoc_url=None, lifespan=lifespan)

    def require_client() -> PlaidWebClient:
        if state.client is None:
            raise RuntimeError("Plaid client not initialized")
        return state.client

    def require_storage() -> PlaidLinkStorage:
        if state.storage is None:
            raise RuntimeError("storage not initialized")
        return state.storage

    def require_secrets() -> SecretStore:
        if state.secrets is None:
            raise RuntimeError("secret store not initialized")
        return state.secrets

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/link", response_class=HTMLResponse)
    async def link_ui() -> str:
        return _LINK_HTML

    @app.get("/link/callback", response_class=HTMLResponse)
    async def link_callback() -> str:
        return _LINK_HTML

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return _LINK_HTML

    # Explicit routes rather than a StaticFiles mount: the assets sit next to app.py, so mounting
    # their directory would also serve the source.
    @app.get("/static/link.css", include_in_schema=False)
    async def link_css() -> Response:
        return Response(_LINK_CSS, media_type="text/css")

    @app.get("/static/link.js", include_in_schema=False)
    async def link_js() -> Response:
        return Response(_LINK_JS, media_type="text/javascript")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/links")
    async def list_links() -> list[LinkSummary]:
        return [_link_summary(link) for link in await require_storage().list_active_links()]

    @app.get("/api/links/{item_id}")
    async def get_link_state(item_id: Annotated[str, ApiPath(description="Plaid item_id")]) -> LinkSummary:
        link = await require_storage().get_link(item_id)
        if link is None:
            raise HTTPException(404, f"unknown item_id: {item_id}")
        return _link_summary(link)

    @app.get("/api/config")
    async def web_config() -> WebConfigResponse:
        return WebConfigResponse(transaction_days=settings.transaction_days)

    @app.get("/api/institutions")
    async def search_institutions(
        q: Annotated[str, Query(min_length=2, description="Institution name typeahead")],
    ) -> list[InstitutionSearchResult]:
        try:
            found = require_client().search_institutions(q)
        except PlaidClientError as exc:
            raise HTTPException(502, exc.public_detail()) from exc
        return [InstitutionSearchResult(institution_id=i.institution_id, name=i.name) for i in found]

    @app.get("/api/institutions/{institution_id}")
    async def get_institution(
        institution_id: Annotated[str, ApiPath(description="Plaid institution_id")],
    ) -> InstitutionProductsResponse:
        try:
            institution = require_client().get_institution(institution_id)
        except PlaidClientError as exc:
            raise HTTPException(502, exc.public_detail()) from exc
        syncable = syncable_products(institution.products)
        return InstitutionProductsResponse(
            institution_id=institution.institution_id,
            name=institution.name,
            url=institution.url,
            syncable_products=syncable,
            unsupported_products=sorted(set(institution.products) - {p.value for p in syncable}),
        )

    @app.post("/api/link-token")
    async def create_link_token(body: LinkTokenRequest) -> LinkTokenResponse:
        try:
            result = require_client().create_link_token(
                products=[product.value for product in body.products],
                institution_id=body.institution_id,
                redirect_uri=settings.redirect_uri,
                client_user_id="owner",
                transaction_days_requested=body.transaction_days_requested or settings.transaction_days,
            )
        except PlaidClientError as exc:
            raise HTTPException(502, exc.public_detail()) from exc
        return LinkTokenResponse(
            link_token=result.link_token,
            products=result.products,
            transaction_days_requested=result.transaction_days_requested,
        )

    @app.post("/api/exchange-public-token")
    async def exchange_public_token(body: ExchangePublicTokenRequest) -> LinkSummary:
        try:
            exchange = require_client().exchange_public_token(body.public_token)
        except PlaidClientError as exc:
            raise HTTPException(502, exc.public_detail()) from exc
        secret_name = _secret_name_for_item(exchange.item_id)
        await require_secrets().write_access_token(secret_name, exchange.access_token)
        requested = body.products
        link = await require_storage().upsert_link(
            item_id=exchange.item_id,
            access_token_secret=secret_name,
            products_requested=requested,
            products_authorized=requested,
            products_billed=[],
            transaction_days_requested=body.transaction_days_requested
            or (settings.transaction_days if Product.TRANSACTIONS.value in requested else None),
            institution_id=body.institution_id,
            institution_name=body.institution_name,
            label=body.label,
        )
        await sync_link(
            api=require_client(),
            storage=require_storage(),
            secrets=require_secrets(),
            link=link,
            trigger="link",
            windows=settings.sync_windows,
        )
        updated = await require_storage().get_link(exchange.item_id)
        if updated is None:
            raise RuntimeError("newly inserted Plaid link disappeared")
        return _link_summary(updated)

    @app.post("/api/links/{item_id}/update-link-token")
    async def create_update_link_token(
        item_id: Annotated[str, ApiPath(description="Plaid item_id")], body: LinkUpdateTokenRequest
    ) -> LinkUpdateTokenResponse:
        link = await require_storage().get_link(item_id)
        if link is None:
            raise HTTPException(404, f"unknown item_id: {item_id}")
        access_token = await require_secrets().read_access_token(link.access_token_secret)
        requested = _requested_products_for_update(link, body)
        additional = [product for product in requested if product not in link.products_authorized]
        try:
            result = require_client().create_update_link_token(
                access_token=access_token,
                redirect_uri=settings.redirect_uri,
                client_user_id="owner",
                additional_products=additional if body.reason == "add_scope" else None,
            )
        except PlaidClientError as exc:
            raise HTTPException(502, exc.public_detail()) from exc
        return LinkUpdateTokenResponse(
            link_token=result.link_token,
            products=_merge_products(link.products_requested, requested),
            additional_products=additional,
        )

    @app.post("/api/links/{item_id}/complete-update")
    async def complete_link_update(
        item_id: Annotated[str, ApiPath(description="Plaid item_id")], body: CompleteLinkUpdateRequest
    ) -> LinkSummary:
        current = await require_storage().get_link(item_id)
        if current is None:
            raise HTTPException(404, f"unknown item_id: {item_id}")
        products = _merge_products(current.products_requested, body.products)
        link = await require_storage().mark_link_update_succeeded(item_id=item_id, products_requested=products)
        if link is None:
            raise HTTPException(404, f"unknown item_id: {item_id}")
        if body.sync:
            await _sync_one_link(api=require_client(), storage=require_storage(), secrets=require_secrets(), link=link)
            refreshed = await require_storage().get_link(item_id)
            if refreshed is not None:
                link = refreshed
        return _link_summary(link)

    @app.post("/api/links/{item_id}/sync")
    async def sync_existing_link(item_id: Annotated[str, ApiPath(description="Plaid item_id")]) -> SyncResponse:
        link = await require_storage().get_link(item_id)
        if link is None:
            raise HTTPException(404, f"unknown item_id: {item_id}")
        run_id = await _sync_one_link(
            api=require_client(), storage=require_storage(), secrets=require_secrets(), link=link
        )
        return SyncResponse(run_id=str(run_id))

    @app.post("/api/links/{item_id}/remove")
    async def remove_link(item_id: Annotated[str, ApiPath(description="Plaid item_id")]) -> dict[str, str]:
        link = await require_storage().get_link(item_id)
        if link is None:
            raise HTTPException(404, f"unknown item_id: {item_id}")
        access_token = await require_secrets().read_access_token(link.access_token_secret)
        try:
            require_client().remove_item(access_token)
        except PlaidClientError as exc:
            raise HTTPException(502, exc.public_detail()) from exc
        await require_secrets().delete_access_token(link.access_token_secret)
        await require_storage().purge_link_data(item_id)
        return {"status": "removed"}

    return app


async def _sync_one_link(
    *, api: PlaidApiLike, storage: PlaidLinkStorage, secrets: SecretStore, link: StoredLink
) -> UUID:
    try:
        return await sync_link(api=api, storage=storage, secrets=secrets, link=link, trigger="manual")
    except RuntimeError as exc:
        if "sync already running" in str(exc):
            raise HTTPException(409, str(exc)) from exc
        raise


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = PlaidWebSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


def _secret_name_for_item(item_id: str) -> str:
    slug = re.sub("[^a-z0-9-]+", "-", item_id.lower()).strip("-")
    return f"plaid-{slug}-access-token"[:253]


def _requested_products_for_update(link: StoredLink, body: LinkUpdateTokenRequest) -> list[str]:
    if body.reason == "repair" or body.products is None:
        return link.products_requested
    return [product.value for product in body.products]


def _merge_products(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for product in group:
            if product not in merged:
                merged.append(product)
    return merged


def _link_summary(link: StoredLink) -> LinkSummary:
    observed_days = None
    if link.earliest_transaction_date is not None:
        observed_days = (datetime.now(UTC).date() - link.earliest_transaction_date).days
    return LinkSummary(
        item_id=link.item_id,
        label=link.label,
        institution_id=link.institution_id,
        institution_name=link.institution_name,
        products_requested=link.products_requested,
        transaction_days_requested=link.transaction_days_requested,
        earliest_transaction_date=link.earliest_transaction_date.isoformat()
        if link.earliest_transaction_date is not None
        else None,
        latest_transaction_date=link.latest_transaction_date.isoformat()
        if link.latest_transaction_date is not None
        else None,
        observed_transaction_history_days=observed_days,
        synced_transaction_count=link.synced_transaction_count,
        products_authorized=link.products_authorized,
        products_billed=link.products_billed,
        status=link.status,
        access_token_secret=link.access_token_secret,
        last_synced_at=link.last_synced_at.isoformat() if link.last_synced_at else None,
    )


if __name__ == "__main__":
    main()
