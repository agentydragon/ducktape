"""FastAPI proxy app that filters Home Assistant REST API by per-token policy."""

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from homeassistant_proxy.config import Settings, TokenConfig
from homeassistant_proxy.policy import AccessDeniedError, PolicyEnforcer

logger = logging.getLogger(__name__)


def _forward(resp: httpx.Response) -> JSONResponse:
    return JSONResponse(resp.json(), status_code=resp.status_code)


def _extract_str_or_list(value: Any, field: str) -> list[str]:
    """Extract a string or list of strings from a request body field."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise HTTPException(status_code=400, detail=f"{field}: expected string or list of strings")


def create_app(settings: Settings) -> FastAPI:
    http_client: httpx.AsyncClient | None = None
    enforcer = PolicyEnforcer(settings.homeassistant.url, settings.homeassistant.token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal http_client
        http_client = httpx.AsyncClient(
            base_url=settings.homeassistant.url,
            headers={"Authorization": f"Bearer {settings.homeassistant.token}"},
            timeout=30.0,
        )
        await enforcer.start()
        yield
        await enforcer.stop()
        await http_client.aclose()
        http_client = None

    def _http() -> httpx.AsyncClient:
        assert http_client is not None, "httpx client not initialized (lifespan not started)"
        return http_client

    app = FastAPI(lifespan=lifespan)

    def _authenticate(request: Request) -> TokenConfig:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer ") :]
            for token_cfg in settings.tokens.values():
                if secrets.compare_digest(token, token_cfg.secret):
                    return token_cfg
        raise HTTPException(status_code=401, detail="unauthorized")

    @app.exception_handler(AccessDeniedError)
    async def _handle_access_denied(request: Request, exc: AccessDeniedError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=403)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Metadata pass-through routes (require auth, no policy filtering).
    async def _passthrough(request: Request) -> JSONResponse:
        _authenticate(request)
        return _forward(await _http().get(request.url.path))

    for path in ("/api/", "/api/config", "/api/services"):
        app.add_api_route(path, _passthrough, methods=["GET"])

    @app.get("/api/states")
    async def api_states(request: Request) -> JSONResponse:
        token_cfg = _authenticate(request)
        resp = await _http().get("/api/states")
        if resp.status_code != 200:
            return _forward(resp)
        states: list[dict[str, Any]] = resp.json()
        all_ids = [s["entity_id"] for s in states]
        allowed = await enforcer.readable_entities(all_ids, token_cfg.policy)
        return JSONResponse([s for s in states if s["entity_id"] in allowed])

    @app.get("/api/states/{entity_id}")
    async def api_state(request: Request, entity_id: str) -> JSONResponse:
        token_cfg = _authenticate(request)
        await enforcer.require_read(entity_id, token_cfg.policy)
        return _forward(await _http().get(f"/api/states/{entity_id}"))

    @app.post("/api/services/{domain}/{service}")
    async def api_call_service(request: Request, domain: str, service: str) -> JSONResponse:
        token_cfg = _authenticate(request)
        body = await request.body()
        data: dict[str, Any] = await request.json() if body else {}

        entity_ids = _extract_str_or_list(data.get("entity_id"), "entity_id")
        device_ids = _extract_str_or_list(data.get("device_id"), "device_id")
        area_ids = _extract_str_or_list(data.get("area_id"), "area_id")

        # Support nested target structure (newer HA format).
        target = data.get("target")
        if isinstance(target, dict):
            entity_ids.extend(_extract_str_or_list(target.get("entity_id"), "target.entity_id"))
            device_ids.extend(_extract_str_or_list(target.get("device_id"), "target.device_id"))
            area_ids.extend(_extract_str_or_list(target.get("area_id"), "target.area_id"))
        elif target is not None:
            raise HTTPException(status_code=400, detail="target: expected object")

        target_entity_ids = await enforcer.resolve_targets(entity_ids, device_ids, area_ids)

        if not target_entity_ids:
            # Services without targets (e.g. homeassistant.restart) are admin-level — block them.
            return JSONResponse({"error": "service call has no entity/device/area target"}, status_code=403)

        await enforcer.require_control(target_entity_ids, token_cfg.policy)
        url = request.url.path
        if request.url.query:
            url = f"{url}?{request.url.query}"
        return _forward(await _http().post(url, content=body, headers={"Content-Type": "application/json"}))

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def catch_all(request: Request, path: str) -> JSONResponse:
        _authenticate(request)
        return JSONResponse({"error": f"endpoint not allowed: /{path}"}, status_code=403)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = create_app(Settings.from_env())
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
