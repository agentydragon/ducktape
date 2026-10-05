"""Build the LiteLLM configuration payloads from the shared model rosters."""

from __future__ import annotations

from dataclasses import dataclass

from cluster.cdk8s.litellm.upstreams import UPSTREAM_BINDINGS
from model_catalog.catalog import HIDDEN_ALIASES, SERVED_ROUTES, Provider, Route, RouteAlias, shape_mode


@dataclass(frozen=True)
class ConfigMapSpec:
    """The generated files belonging to one LiteLLM proxy."""

    name: str
    namespace: str
    data: dict[str, object]

    @property
    def config_map_name(self) -> str:
        return "config"


def model_entry(entry: Route | RouteAlias) -> dict:
    """Project a served route into LiteLLM's schema without choosing its identity."""
    route = entry.target if isinstance(entry, RouteAlias) else entry
    upstream = route.upstream
    binding = UPSTREAM_BINDINGS[upstream]
    params: dict = {"model": route.upstream_id}
    if binding.api_base is not None:
        params["api_base"] = binding.api_base
    if binding.api_key is not None:
        params["api_key"] = binding.api_key
    if route.num_ctx is not None and route.num_ctx != 128 * 1024:
        params["extra_body"] = {"options": {"num_ctx": route.num_ctx}}
    if upstream.provider == Provider.TANA:
        params.update(
            timeout=60,
            custom_llm_provider=Provider.TANA,
            firebase_api_key="AIzaSyA9LtJM6Ga9VAwCfj9w_mNORdOaq2yLshQ",
            tana_user_context="Generic AI Query",
            tana_tool_user_context="Ask Tana",
            tana_ignore_large_context_warning=True,
            tana_ignore_out_of_credits_warning=False,
        )
    info: dict = {"mode": shape_mode(upstream.shape)}
    if upstream.supports_function_calling:
        info["supports_function_calling"] = True
    if route.publish_limits:
        model = route.model
        if model.context_window is None or model.max_output_tokens is None:
            raise ValueError(f"cannot publish unknown limits for {route.id}")
        info.update(max_input_tokens=model.context_window, max_output_tokens=model.max_output_tokens)
    return {"model_name": entry.id, "litellm_params": params, "model_info": info}


def main_proxy_config() -> dict:
    """Return the complete main-proxy config from the shared Python roster."""
    return {
        "model_list": [model_entry(route) for route in SERVED_ROUTES],
        "litellm_settings": {
            "drop_params": True,
            "callbacks": ["langfuse_otel", "prometheus"],
            "custom_provider_map": [
                {"provider": "tana", "custom_handler": "tana.litellm_proxy.custom_handler.tana_handler"}
            ],
        },
        "router_settings": {
            # Codex 0.153+ bundles metadata for this exact slug (272k base / 872k
            # configurable maximum). Keep the alias hidden so Codex can select the
            # recognized slug while requests still use the Responses-only route above.
            "model_group_alias": {alias.id: {"model": alias.target.id, "hidden": True} for alias in HIDDEN_ALIASES}
        },
        "general_settings": {
            # Forward the client's `anthropic-beta` and `x-*` headers upstream -- never
            # User-Agent, which LiteLLM has no setting for, so CLIProxyAPI cannot confirm a
            # native Claude Code caller and rebuilds the beta set from the request body
            # instead. A few betas are request-only and reach it no other way, above all
            # context-1m-2025-08-07, which nothing in the body implies. Applies to every
            # client and every upstream, not only the Claude lanes.
            "forward_client_headers_to_llm_api": True,
            "store_model_in_db": False,
        },
    }


def proxy_configs() -> tuple[ConfigMapSpec, ...]:
    return (ConfigMapSpec("litellm", "litellm", {"config.yaml": main_proxy_config()}),)
