"""Loading an Ollama-served model before the first turn that needs it.

A cold load takes about a minute, which a turn's read timeout does not forgive. The calls go to
Ollama directly, so they work from a Sandbox whose egress policy allows `/api/ps` and `/api/generate`
on Ollama (`inference-experiments`); from anywhere else the warm-up is skipped with a warning.
"""

import logging

import httpx

from model_catalog.catalog import OLLAMA_CHAT_ROUTES

logger = logging.getLogger(__name__)

# In-cluster address and the egress sidecar's placeholder for the Ollama credential.
OLLAMA_URL = "http://ollama.ollama.svc.cluster.local:11435"
OLLAMA_CREDENTIAL_PLACEHOLDER = "agentplane-credential-ollama"

CONNECT_SECONDS = 5.0
LOAD_SECONDS = 600.0
KEEP_ALIVE = "30m"


async def warm_ollama(model: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Load `model` into Ollama unless it already is. Routes Ollama does not serve are left alone."""
    route = next((route for route in OLLAMA_CHAT_ROUTES if route.id == model), None)
    if route is None:
        return
    async with httpx.AsyncClient(
        base_url=OLLAMA_URL,
        headers={"Authorization": f"Bearer {OLLAMA_CREDENTIAL_PLACEHOLDER}"},
        timeout=httpx.Timeout(LOAD_SECONDS, connect=CONNECT_SECONDS),
        transport=transport,
    ) as ollama:
        try:
            loaded = await ollama.get("/api/ps")
            loaded.raise_for_status()
            if any(entry["name"] == route.upstream_model for entry in loaded.json()["models"]):
                return
            # An empty prompt loads the model without generating.
            options = {} if route.num_ctx is None else {"num_ctx": route.num_ctx}
            loading = await ollama.post(
                "/api/generate",
                json={
                    "model": route.upstream_model,
                    "prompt": "",
                    "keep_alive": KEEP_ALIVE,
                    "stream": False,
                    "options": options,
                },
            )
            loading.raise_for_status()
        except httpx.HTTPError:
            logger.warning("could not warm Ollama model %s; its first turn may be slow", model, exc_info=True)
