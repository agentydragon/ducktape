"""FastAPI proxy app that filters Home Assistant REST API by per-token policy."""

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from homeassistant_proxy.config import Action, Settings, TokenConfig
from homeassistant_proxy.policy import EntityInfo, check_all_entities, filter_entities
from homeassistant_proxy.registry import fetch_registry

logger = logging.getLogger(__name__)


def _forward(resp: httpx.Response) -> JSONResponse:
    return JSONResponse(resp.json(), status_code=resp.status_code)


def _resolve_ids(raw: str | list[str] | None, registry: dict[str, EntityInfo], attr: str) -> list[str]:
    """Resolve a body field (device_id or area_id) to entity_ids via registry."""
    ids: list[str] = [raw] if isinstance(raw, str) else (raw or [])
    return [info.entity_id for id_ in ids for info in registry.values() if getattr(info, attr) == id_]


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

    async def _require_auth(request: Request) -> TokenConfig:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer ") :]
            for token_cfg in settings.tokens.values():
                if secrets.compare_digest(token, token_cfg.secret):
                    return token_cfg
        raise HTTPException(status_code=401, detail="unauthorized")

    Auth = Annotated[TokenConfig, Depends(_require_auth)]  # noqa: N806

    async def _get_registry() -> dict[str, EntityInfo]:
        return await fetch_registry(settings.homeassistant.url, settings.homeassistant.token)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/")
    async def api_status(_: Auth) -> JSONResponse:
        return _forward(await _http().get("/api/"))

    @app.get("/api/config")
    async def api_config(_: Auth) -> JSONResponse:
        return _forward(await _http().get("/api/config"))

    @app.get("/api/services")
    async def api_services(_: Auth) -> JSONResponse:
        return _forward(await _http().get("/api/services"))

    @app.get("/api/states")
    async def api_states(token_cfg: Auth) -> JSONResponse:
        resp = await _http().get("/api/states")
        if resp.status_code != 200:
            return _forward(resp)
        registry = await _get_registry()
        states: list[dict[str, Any]] = resp.json()
        all_ids = [s["entity_id"] for s in states]
        allowed = set(filter_entities(all_ids, Action.READ, token_cfg.policy, registry))
        return JSONResponse([s for s in states if s["entity_id"] in allowed])

    @app.get("/api/states/{entity_id}")
    async def api_state(token_cfg: Auth, entity_id: str) -> JSONResponse:
        registry = await _get_registry()
        denied = check_all_entities([entity_id], Action.READ, token_cfg.policy, registry)
        if denied:
            return JSONResponse({"error": f"access denied for entity: {entity_id}"}, status_code=403)
        return _forward(await _http().get(f"/api/states/{entity_id}"))

    @app.post("/api/services/{domain}/{service}")
    async def api_call_service(token_cfg: Auth, request: Request, domain: str, service: str) -> JSONResponse:
        body = await request.body()
        data: dict[str, Any] = await request.json() if body else {}

        # Extract target entity IDs from the body.
        # The REST API uses flat entity_id in the body.
        target_entity_ids: list[str] = []
        raw_eid = data.get("entity_id")
        if isinstance(raw_eid, str):
            target_entity_ids.append(raw_eid)
        elif isinstance(raw_eid, list):
            target_entity_ids.extend(raw_eid)

        # Resolve device_id/area_id targets to entity_ids via registry.
        registry = await _get_registry()
        target_entity_ids.extend(_resolve_ids(data.get("device_id"), registry, "device_id"))
        target_entity_ids.extend(_resolve_ids(data.get("area_id"), registry, "area_id"))

        if not target_entity_ids:
            # Services without targets (e.g. homeassistant.restart) are admin-level — block them.
            return JSONResponse({"error": "service call has no entity/device/area target"}, status_code=403)

        denied = check_all_entities(target_entity_ids, Action.CONTROL, token_cfg.policy, registry)
        if denied:
            return JSONResponse({"error": f"control denied for entities: {denied}"}, status_code=403)
        url = f"/api/services/{domain}/{service}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        return _forward(await _http().post(url, content=body, headers={"Content-Type": "application/json"}))

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def catch_all(_: Auth, path: str) -> JSONResponse:
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
