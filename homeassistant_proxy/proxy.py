"""FastAPI proxy app that filters Home Assistant REST API by per-token policy."""

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from homeassistant_proxy.config import Action, Settings, TokenConfig
from homeassistant_proxy.policy import EntityInfo, check_all_entities, filter_entities
from homeassistant_proxy.registry import fetch_registry

logger = logging.getLogger(__name__)

# Endpoints that are always blocked (admin-only in HA).
_BLOCKED_PREFIXES = ("/api/events", "/api/stream", "/api/template", "/api/error_log", "/api/camera_proxy")


def _make_app(settings: Settings) -> FastAPI:
    http_client: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal http_client
        http_client = httpx.AsyncClient(
            base_url=settings.homeassistant.url,
            headers={"Authorization": f"Bearer {settings.homeassistant.token}"},
            timeout=30.0,
        )
        yield
        await http_client.aclose()
        http_client = None

    def _http() -> httpx.AsyncClient:
        assert http_client is not None, "httpx client not initialized (lifespan not started)"
        return http_client

    app = FastAPI(lifespan=lifespan)

    def _authenticate(request: Request) -> TokenConfig | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer ") :]
        for token_cfg in settings.tokens.values():
            if secrets.compare_digest(token, token_cfg.secret):
                return token_cfg
        return None

    async def _get_registry() -> dict[str, EntityInfo]:
        return await fetch_registry(settings.homeassistant.url, settings.homeassistant.token)

    async def _proxy_get(http: httpx.AsyncClient, path: str) -> httpx.Response:
        return await http.get(path)

    async def _proxy_post(http: httpx.AsyncClient, path: str, body: bytes, query: str) -> httpx.Response:
        url = path if not query else f"{path}?{query}"
        return await http.post(url, content=body, headers={"Content-Type": "application/json"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/")
    async def api_status(request: Request) -> JSONResponse:
        token_cfg = _authenticate(request)
        if not token_cfg:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        resp = await _proxy_get(_http(), "/api/")
        return JSONResponse(resp.json(), status_code=resp.status_code)

    @app.get("/api/config")
    async def api_config(request: Request) -> JSONResponse:
        token_cfg = _authenticate(request)
        if not token_cfg:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        resp = await _proxy_get(_http(), "/api/config")
        return JSONResponse(resp.json(), status_code=resp.status_code)

    @app.get("/api/services")
    async def api_services(request: Request) -> JSONResponse:
        token_cfg = _authenticate(request)
        if not token_cfg:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        resp = await _proxy_get(_http(), "/api/services")
        return JSONResponse(resp.json(), status_code=resp.status_code)

    @app.get("/api/states")
    async def api_states(request: Request) -> JSONResponse:
        token_cfg = _authenticate(request)
        if not token_cfg:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        resp = await _proxy_get(_http(), "/api/states")
        if resp.status_code != 200:
            return JSONResponse(resp.json(), status_code=resp.status_code)
        registry = await _get_registry()
        states: list[dict[str, Any]] = resp.json()
        all_ids = [s["entity_id"] for s in states]
        allowed = set(filter_entities(all_ids, Action.READ, token_cfg.policy, registry))
        filtered = [s for s in states if s["entity_id"] in allowed]
        return JSONResponse(filtered)

    @app.get("/api/states/{entity_id}")
    async def api_state(request: Request, entity_id: str) -> JSONResponse:
        token_cfg = _authenticate(request)
        if not token_cfg:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        registry = await _get_registry()
        denied = check_all_entities([entity_id], Action.READ, token_cfg.policy, registry)
        if denied:
            return JSONResponse({"error": f"access denied for entity: {entity_id}"}, status_code=403)
        resp = await _proxy_get(_http(), f"/api/states/{entity_id}")
        return JSONResponse(resp.json(), status_code=resp.status_code)

    @app.post("/api/services/{domain}/{service}")
    async def api_call_service(request: Request, domain: str, service: str) -> JSONResponse:
        token_cfg = _authenticate(request)
        if not token_cfg:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.body()
        data: dict[str, Any] = {}
        if body:
            data = await request.json()
        # Extract target entity IDs from the body.
        # The REST API uses flat entity_id in the body.
        # Also handle device_id/area_id if present (resolve via registry).
        target_entity_ids: list[str] = []
        raw_eid = data.get("entity_id")
        if isinstance(raw_eid, str):
            target_entity_ids.append(raw_eid)
        elif isinstance(raw_eid, list):
            target_entity_ids.extend(raw_eid)

        registry = await _get_registry()

        # Resolve device_id targets to entity_ids
        raw_did = data.get("device_id")
        device_ids: list[str] = [raw_did] if isinstance(raw_did, str) else (raw_did or [])
        for did in device_ids:
            for info in registry.values():
                if info.device_id == did:
                    target_entity_ids.append(info.entity_id)

        # Resolve area_id targets to entity_ids
        raw_aid = data.get("area_id")
        area_ids: list[str] = [raw_aid] if isinstance(raw_aid, str) else (raw_aid or [])
        for aid in area_ids:
            for info in registry.values():
                if info.area_id == aid:
                    target_entity_ids.append(info.entity_id)

        # If no targets specified, the service may affect everything.
        # Deny by default for safety — require explicit targets.
        if not target_entity_ids:
            # Some services don't target entities (e.g. homeassistant.restart).
            # Block them — they're admin-level.
            return JSONResponse({"error": "service call has no entity/device/area target"}, status_code=403)

        denied = check_all_entities(target_entity_ids, Action.CONTROL, token_cfg.policy, registry)
        if denied:
            return JSONResponse({"error": f"control denied for entities: {denied}"}, status_code=403)
        resp = await _proxy_post(_http(), f"/api/services/{domain}/{service}", body, request.url.query)
        return JSONResponse(resp.json(), status_code=resp.status_code)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def catch_all(request: Request, path: str) -> JSONResponse:
        token_cfg = _authenticate(request)
        if not token_cfg:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"error": f"endpoint not allowed: /{path}"}, status_code=403)

    return app


def create_app() -> FastAPI:
    return _make_app(Settings.from_env())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
