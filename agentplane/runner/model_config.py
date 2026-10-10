"""Look up the deployed LLM ingress's client configuration for one exposed route."""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from agentplane.llm_ingress.models import ModelConfig


class ModelConfigLookupError(RuntimeError):
    """The ingress did not return valid model configuration."""


class HttpModelConfigResolver:
    """Query the workload-authenticated LLM ingress, using the runner process's egress settings."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def resolve(self, *, base_url: str, token: str, model: str) -> ModelConfig | None:
        try:
            endpoint = httpx.URL(base_url).copy_with(path="/agentplane/model-config", query=None, fragment=None)
        except httpx.InvalidURL as error:
            raise ModelConfigLookupError("model endpoint must be an absolute HTTP(S) URL") from error
        if endpoint.scheme not in {"http", "https"} or not endpoint.host:
            raise ModelConfigLookupError("model endpoint must be an absolute HTTP(S) URL")
        try:
            async with httpx.AsyncClient(timeout=10, transport=self.transport) as client:
                response = await client.get(
                    endpoint, params={"model": model}, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.HTTPError as error:
            raise ModelConfigLookupError("could not query the LLM proxy for model configuration") from error
        if response.status_code == 404:
            try:
                detail = response.json().get("detail")
            except ValueError, AttributeError:
                detail = None
            if detail == "no configuration for model":
                return None
            raise ModelConfigLookupError("LLM proxy model-config lookup endpoint is unavailable")
        if response.status_code != 200:
            raise ModelConfigLookupError(f"LLM proxy model-config lookup failed with HTTP {response.status_code}")
        try:
            payload = ModelConfig.model_validate_json(response.content)
        except ValidationError as error:
            raise ModelConfigLookupError("LLM proxy returned malformed model configuration") from error
        if payload.model != model:
            raise ModelConfigLookupError("LLM proxy returned invalid model configuration")
        return payload
