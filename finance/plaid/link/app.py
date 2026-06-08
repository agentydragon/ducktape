"""Plaid v0 web app: Link UI plus synchronous full-refresh sync."""

from __future__ import annotations

import contextlib
import logging
import re
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Path as ApiPath
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from finance.plaid.db.client import LinkTokenResult, PlaidClient, PlaidClientError, PlaidCreds, PublicTokenExchange
from finance.plaid.db.config import MAX_TRANSACTION_DAYS, PlaidWebSettings
from finance.plaid.db.link_profiles import LinkProfile, Product, products_for_profile
from finance.plaid.db.link_store import PlaidLinkStorage, StoredLink
from finance.plaid.db.secret_store import K8sSecretStore, SecretStore
from finance.plaid.db.sync import PlaidApiLike, sync_link

logger = logging.getLogger(__name__)


class LinkTokenRequest(BaseModel):
    profile: LinkProfile
    advanced_products: list[str] | None = None
    transaction_days_requested: int | None = Field(default=None, ge=1, le=MAX_TRANSACTION_DAYS)


class LinkTokenResponse(BaseModel):
    link_token: str
    products: list[str]
    transaction_days_requested: int | None


class WebConfigResponse(BaseModel):
    transaction_days: int
    max_transaction_days: int = MAX_TRANSACTION_DAYS


class LinkUpdateTokenRequest(BaseModel):
    reason: Literal["repair", "add_scope"] = "repair"
    profile: LinkProfile | None = None
    advanced_products: list[str] | None = None


class LinkUpdateTokenResponse(BaseModel):
    link_token: str
    products: list[str]
    additional_products: list[str]


class CompleteLinkUpdateRequest(BaseModel):
    profile: LinkProfile | None = None
    products: list[str] = Field(default_factory=list)
    sync: bool = True


class SyncResponse(BaseModel):
    run_id: str


class ExchangePublicTokenRequest(BaseModel):
    public_token: str
    profile: LinkProfile
    products: list[str]
    transaction_days_requested: int | None = Field(default=None, ge=1, le=MAX_TRANSACTION_DAYS)
    label: str | None = None
    institution_id: str | None = None
    institution_name: str | None = None


class LinkSummary(BaseModel):
    item_id: str
    label: str | None
    institution_id: str | None
    institution_name: str | None
    link_profile: LinkProfile
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
        profile: LinkProfile,
        redirect_uri: str,
        client_user_id: str,
        advanced_products: list[str] | None = None,
        transaction_days_requested: int = 730,
        client_name: str = "Plaid MCP",
    ) -> LinkTokenResult: ...
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

    @app.post("/api/link-token")
    async def create_link_token(body: LinkTokenRequest) -> LinkTokenResponse:
        try:
            result = require_client().create_link_token(
                profile=body.profile,
                redirect_uri=settings.redirect_uri,
                client_user_id="owner",
                advanced_products=body.advanced_products,
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
        requested = body.products or products_for_profile(body.profile)
        link = await require_storage().upsert_link(
            item_id=exchange.item_id,
            access_token_secret=secret_name,
            link_profile=body.profile,
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
        profile = body.profile or current.link_profile
        link = await require_storage().mark_link_update_succeeded(
            item_id=item_id, link_profile=profile, products_requested=products
        )
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
    if body.reason == "repair" or body.profile is None:
        return link.products_requested
    return products_for_profile(body.profile, body.advanced_products)


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
        link_profile=link.link_profile,
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


_LINK_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Plaid Links</title>
    <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
    <style>
      :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
      *, *::before, *::after { box-sizing: border-box; }
      body { margin: 0; background: Canvas; color: CanvasText; }
      main { max-width: 1120px; margin: 0 auto; padding: 32px 20px; }
      header { display: flex; justify-content: space-between; gap: 16px; align-items: center; flex-wrap: wrap; }
      h1 { font-size: 28px; margin: 0; }
      h2 { font-size: 18px; margin: 0 0 12px; }
      section { margin-top: 24px; }
      form { display: grid; grid-template-columns: minmax(160px, 1fr) minmax(220px, 1fr) minmax(170px, 1fr) auto; gap: 12px; align-items: end; }
      label { display: grid; gap: 6px; font-size: 13px; color: color-mix(in srgb, CanvasText 78%, Canvas); }
      input, select, button { font: inherit; height: 38px; padding: 8px 10px; border-radius: 6px; border: 1px solid color-mix(in srgb, CanvasText 22%, Canvas); background: Canvas; color: CanvasText; }
      button { cursor: pointer; background: #276ef1; color: white; border-color: #276ef1; font-weight: 650; white-space: nowrap; }
      button.secondary { background: Canvas; color: CanvasText; border-color: color-mix(in srgb, CanvasText 24%, Canvas); }
      button.danger { background: #b42318; border-color: #b42318; color: white; }
      button:disabled { opacity: 0.6; cursor: wait; }
      .advanced { display: none; grid-column: 1 / -1; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
      .advanced.visible { display: grid; }
      .check { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid color-mix(in srgb, CanvasText 16%, Canvas); border-radius: 6px; color: CanvasText; }
      .check input { padding: 0; height: auto; }
      .status { min-height: 22px; margin-top: 14px; color: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 14px; }
      .table-wrap { overflow-x: auto; }
      table { width: 100%; min-width: 980px; border-collapse: collapse; margin-top: 12px; }
      th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid color-mix(in srgb, CanvasText 14%, Canvas); font-size: 14px; vertical-align: top; }
      th { font-size: 12px; text-transform: uppercase; color: color-mix(in srgb, CanvasText 58%, Canvas); letter-spacing: 0; }
      .name { font-weight: 700; }
      .muted { color: color-mix(in srgb, CanvasText 62%, Canvas); }
      .meta { margin-top: 3px; font-size: 12px; overflow-wrap: anywhere; }
      .hidden { display: none; }
      .pill-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
      .pill { border: 1px solid color-mix(in srgb, CanvasText 16%, Canvas); border-radius: 999px; padding: 3px 8px; font-size: 12px; background: color-mix(in srgb, CanvasText 5%, Canvas); }
      .actions { display: grid; grid-template-columns: minmax(180px, 1fr) repeat(4, auto); gap: 8px; align-items: center; }
      .empty { padding: 18px 8px; color: color-mix(in srgb, CanvasText 62%, Canvas); }
      @media (max-width: 820px) {
        form { grid-template-columns: 1fr; }
        .advanced { grid-template-columns: 1fr; }
        table { min-width: 760px; }
        .actions { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <h1>Plaid Links</h1>
          <div class="muted">Manage linked institutions and product profiles.</div>
        </div>
      </header>
      <section>
        <h2>Connect Institution</h2>
        <form id="link-form">
          <label>Label <input id="label" placeholder="Chase personal" /></label>
          <label>Data surface
            <select id="profile">
              <option value="cashflow">Cashflow</option>
              <option value="credit_card_detail">Credit card detail</option>
              <option value="investments_holdings">Investment holdings</option>
              <option value="investments_full">Investments full</option>
              <option value="full_picture">Full picture</option>
              <option value="advanced">Advanced</option>
            </select>
          </label>
          <label id="transaction-days-wrap">History days <input id="transaction-days" type="number" min="1" max="730" step="1" /></label>
          <div id="advanced-products" class="advanced">
            <label class="check"><input type="checkbox" value="transactions" checked />Transactions</label>
            <label class="check"><input type="checkbox" value="investments" />Investments</label>
            <label class="check"><input type="checkbox" value="liabilities" />Liabilities</label>
          </div>
          <button type="submit">Connect</button>
        </form>
        <div id="status" class="status" role="status"></div>
      </section>
      <section>
        <h2>Active Links</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Institution</th><th>Access</th><th>Sync</th><th>Actions</th></tr></thead>
            <tbody id="links"></tbody>
          </table>
        </div>
      </section>
    </main>
    <script>
      const pendingKey = 'plaid-link-pending';
      let webConfig = {transaction_days: 730, max_transaction_days: 730};
      const profiles = [
        ['cashflow', 'Cashflow'],
        ['credit_card_detail', 'Credit card detail'],
        ['investments_holdings', 'Investment holdings'],
        ['investments_full', 'Investments full'],
        ['full_picture', 'Full picture']
      ];
      const profileProducts = {
        cashflow: ['transactions'],
        credit_card_detail: ['transactions', 'liabilities'],
        investments_holdings: ['investments'],
        investments_full: ['investments'],
        full_picture: ['transactions', 'investments', 'liabilities']
      };
      const statusEl = document.getElementById('status');

      function setStatus(message) {
        statusEl.textContent = message || '';
      }
      function escapeHtml(value) {
        return String(value || '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
      }
      function pills(products) {
        if (!products || products.length === 0) return '<span class="muted">none recorded</span>';
        return `<div class="pill-row">${products.map(product => `<span class="pill">${escapeHtml(product)}</span>`).join('')}</div>`;
      }
      function profileSelect(link) {
        const current = link.link_profile || 'cashflow';
        return `<select data-role="scope-profile">${profiles.map(([value, label]) => `<option value="${value}" ${value === current ? 'selected' : ''}>${label}</option>`).join('')}</select>`;
      }
      function selectedProducts() {
        const profile = document.getElementById('profile').value;
        if (profile === 'advanced') return advancedProducts();
        return profileProducts[profile] || [];
      }
      function historySummary(link) {
        const hasTransactions = (link.products_requested || []).includes('transactions') || (link.products_authorized || []).includes('transactions');
        if (!hasTransactions) return '<span class="muted">No transaction history requested.</span>';
        const requested = link.transaction_days_requested === null || link.transaction_days_requested === undefined
          ? 'Requested: unknown'
          : `Requested: ${escapeHtml(link.transaction_days_requested)} days`;
        const count = Number(link.synced_transaction_count || 0).toLocaleString();
        if (link.observed_transaction_history_days === null || link.observed_transaction_history_days === undefined) {
          return `${requested}<div class="meta muted">Observed: no synced transactions yet</div>`;
        }
        const dates = link.earliest_transaction_date && link.latest_transaction_date
          ? `, ${escapeHtml(link.earliest_transaction_date)} to ${escapeHtml(link.latest_transaction_date)}`
          : '';
        return `${requested}<div class="meta muted">Observed: ${escapeHtml(link.observed_transaction_history_days)} days${dates}, ${count} transactions</div>`;
      }
      async function apiFetch(url, options) {
        const response = await fetch(url, options);
        const contentType = response.headers.get('content-type') || '';
        const body = contentType.includes('application/json') ? await response.json() : await response.text();
        if (!response.ok) {
          throw new Error(apiErrorMessage(body, response));
        }
        return body;
      }
      function apiErrorMessage(body, response) {
        const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : body;
        if (detail && typeof detail === 'object') {
          const bits = [];
          if (detail.error_code) bits.push(detail.error_code);
          if (detail.error_message) bits.push(detail.error_message);
          if (detail.request_id) bits.push(`request ${detail.request_id}`);
          if (bits.length) return bits.join(': ');
          return JSON.stringify(detail);
        }
        return String(detail || `${response.status} ${response.statusText}`);
      }
      async function withStatus(message, work) {
        setStatus(message);
        document.querySelectorAll('button').forEach(button => { button.disabled = true; });
        try {
          const result = await work();
          return result;
        } catch (error) {
          setStatus(error.message || String(error));
          throw error;
        } finally {
          document.querySelectorAll('button').forEach(button => { button.disabled = false; });
        }
      }
      async function loadConfig() {
        webConfig = await apiFetch('/api/config');
        const input = document.getElementById('transaction-days');
        input.max = String(webConfig.max_transaction_days);
        input.value = String(webConfig.transaction_days);
      }

      async function refreshLinks() {
        const links = await apiFetch('/api/links');
        const tbody = document.getElementById('links');
        tbody.innerHTML = '';
        if (links.length === 0) {
          tbody.innerHTML = '<tr><td class="empty" colspan="4">No active Plaid links.</td></tr>';
          return;
        }
        for (const link of links) {
          const tr = document.createElement('tr');
          tr.dataset.item = link.item_id;
          tr.innerHTML = `
            <td>
              <div class="name">${escapeHtml(link.label || link.institution_name || link.item_id)}</div>
              <div class="muted">${escapeHtml(link.institution_name || '')}</div>
              <div class="meta muted">${escapeHtml(link.item_id)}</div>
              <div class="meta">Status: ${escapeHtml(link.status)}</div>
            </td>
            <td>
              <div>Requested ${pills(link.products_requested)}</div>
              <div class="meta">${historySummary(link)}</div>
              <div class="meta muted">Authorized ${pills(link.products_authorized)}</div>
              <div class="meta muted">Billed ${pills(link.products_billed)}</div>
            </td>
            <td>
              <div>${escapeHtml(link.last_synced_at || 'not synced yet')}</div>
              <div class="meta muted">Secret: ${escapeHtml(link.access_token_secret)}</div>
            </td>
            <td>
              <div class="actions">
                ${profileSelect(link)}
                <button class="secondary" data-action="update">Add scopes</button>
                <button class="secondary" data-action="repair">Repair</button>
                <button class="secondary" data-action="sync">Sync</button>
                <button class="danger" data-action="remove">Remove</button>
              </div>
            </td>`;
          tbody.appendChild(tr);
        }
      }
      function advancedProducts() {
        return Array.from(document.querySelectorAll('#advanced-products input:checked')).map(input => input.value);
      }
      function setAdvancedVisibility() {
        const isAdvanced = document.getElementById('profile').value === 'advanced';
        document.getElementById('advanced-products').classList.toggle('visible', isAdvanced);
        document.getElementById('transaction-days-wrap').classList.toggle('hidden', !selectedProducts().includes('transactions'));
      }
      async function exchangePublicToken(public_token, metadata, pending) {
        try {
          await apiFetch('/api/exchange-public-token', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({
              public_token,
              profile: pending.profile,
              products: pending.products,
              transaction_days_requested: pending.transaction_days_requested || null,
              label: pending.label,
              institution_id: metadata.institution?.institution_id || null,
              institution_name: metadata.institution?.name || null
            })
          });
          setStatus('Link connected and synced.');
        } catch (error) {
          // The link row is persisted before the post-link sync runs, so a sync
          // failure still leaves a usable link — surface the error but keep the row.
          setStatus(`Link connected, but sync failed: ${error.message || error}`);
        } finally {
          sessionStorage.removeItem(pendingKey);
          await refreshLinks();
        }
      }
      async function completeUpdate(metadata, pending) {
        try {
          await apiFetch(`/api/links/${encodeURIComponent(pending.item_id)}/complete-update`, {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({profile: pending.profile, products: pending.products, sync: true})
          });
          setStatus(metadata?.institution?.name ? `Updated ${metadata.institution.name}.` : 'Link updated and synced.');
        } catch (error) {
          setStatus(`Link updated, but sync failed: ${error.message || error}`);
        } finally {
          sessionStorage.removeItem(pendingKey);
          await refreshLinks();
        }
      }
      function openPlaid(pending, receivedRedirectUri) {
        const handler = Plaid.create({
          token: pending.link_token,
          receivedRedirectUri,
          onSuccess: async (public_token, metadata) => {
            if (pending.mode === 'update') {
              await completeUpdate(metadata, pending);
            } else {
              await exchangePublicToken(public_token, metadata, pending);
            }
          },
          onExit: (error) => {
            if (error) setStatus(error.display_message || error.error_message || 'Plaid Link exited with an error.');
          }
        });
        handler.open();
      }
      document.getElementById('links').addEventListener('click', async (event) => {
        const button = event.target.closest('button[data-action]');
        if (!button) return;
        const row = button.closest('tr[data-item]');
        const item = row?.dataset?.item;
        const action = button.dataset.action;
        if (!item || !action) return;
        if (action === 'remove') {
          if (!window.confirm('Remove this Plaid link and delete its access-token Secret?')) return;
          await withStatus('Removing link...', async () => {
            await apiFetch(`/api/links/${encodeURIComponent(item)}/remove`, {method: 'POST'});
            await refreshLinks();
            setStatus('Link removed.');
          });
          return;
        }
        if (action === 'sync') {
          await withStatus('Syncing link...', async () => {
            const result = await apiFetch(`/api/links/${encodeURIComponent(item)}/sync`, {method: 'POST'});
            await refreshLinks();
            setStatus(`Sync completed: ${result.run_id}`);
          });
          return;
        }
        const body = action === 'repair'
          ? {reason: 'repair'}
          : {reason: 'add_scope', profile: row.querySelector('[data-role="scope-profile"]').value};
        await withStatus(action === 'repair' ? 'Opening Plaid repair flow...' : 'Opening Plaid scope request...', async () => {
          const token = await apiFetch(`/api/links/${encodeURIComponent(item)}/update-link-token`, {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify(body)
          });
          const pending = {
            mode: 'update',
            item_id: item,
            profile: body.profile || null,
            products: token.products,
            link_token: token.link_token
          };
          sessionStorage.setItem(pendingKey, JSON.stringify(pending));
          openPlaid(pending);
        });
      });
      document.getElementById('profile').addEventListener('change', setAdvancedVisibility);
      document.getElementById('advanced-products').addEventListener('change', setAdvancedVisibility);
      document.getElementById('link-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        await withStatus('Creating Plaid Link session...', async () => {
          const profile = document.getElementById('profile').value;
          const label = document.getElementById('label').value || null;
          const advanced_products = profile === 'advanced' ? advancedProducts() : null;
          const selected_products = selectedProducts();
          const transaction_days_requested = selected_products.includes('transactions') ? Number(document.getElementById('transaction-days').value) : null;
          const token = await apiFetch('/api/link-token', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({profile, advanced_products, transaction_days_requested})});
          const pending = {mode: 'new', profile, products: token.products, transaction_days_requested: token.transaction_days_requested, label, link_token: token.link_token};
          sessionStorage.setItem(pendingKey, JSON.stringify(pending));
          openPlaid(pending);
        });
      });
      async function init() {
        await loadConfig();
        setAdvancedVisibility();
        await refreshLinks();
        const pending = JSON.parse(sessionStorage.getItem(pendingKey) || 'null');
        if (pending && new URLSearchParams(window.location.search).has('oauth_state_id')) {
          setStatus('Completing Plaid redirect...');
          openPlaid(pending, window.location.href);
        }
      }
      init().catch(error => setStatus(error.message || String(error)));
    </script>
  </body>
</html>
"""


if __name__ == "__main__":
    main()
