"""Build the LiteLLM configuration payloads from the shared model rosters."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from cluster.cdk8s.model_rosters import (
    ANTHROPIC_MODELS,
    ANTIGRAVITY_MODELS,
    CLIPROXY_MODELS,
    GEMINI_EMBEDDING_COMPAT_ALIAS,
    GEMINI_EMBEDDING_MODELS,
    GEMINI_MODELS,
    MISTRAL_MODELS,
    OLLAMA_CHAT_MODELS,
    OLLAMA_EMBEDDING_MODEL,
    OPENCLAW_CODEX_MODELS,
    TANA_MODELS,
    Provider,
    codex_responses_name,
    exposed_name,
    ollama_chat_variant,
    shape_for,
    shape_mode,
)

_OLLAMA_BASE = "http://ollama.ollama.svc.cluster.local:11434"
_CLIPROXY_BASE = "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317"


@dataclass(frozen=True)
class ConfigMapSpec:
    """The generated files belonging to one LiteLLM proxy."""

    name: str
    namespace: str
    data: dict[str, object]

    @property
    def config_map_name(self) -> str:
        return f"{self.name}-config"


def _model_entry(
    model_name: str,
    model: str,
    mode: str,
    *,
    api_base: str | None = None,
    api_key: str | None = None,
    timeout: int | float | None = None,
    supports_function_calling: bool = False,
    model_info: dict[str, int] | None = None,
    extra_body: dict | None = None,
    extra_litellm_params: dict[str, object] | None = None,
    custom_llm_provider: str | None = None,
) -> dict:
    """Build one LiteLLM model entry while omitting unset optional fields."""
    litellm_params: dict = {"model": model}
    if api_base is not None:
        litellm_params["api_base"] = api_base
    if api_key is not None:
        litellm_params["api_key"] = api_key
    if timeout is not None:
        litellm_params["timeout"] = timeout
    if extra_body is not None:
        litellm_params["extra_body"] = extra_body
    if custom_llm_provider is not None:
        litellm_params["custom_llm_provider"] = custom_llm_provider
    if extra_litellm_params is not None:
        overlap = litellm_params.keys() & extra_litellm_params.keys()
        if overlap:
            raise ValueError(f"extra LiteLLM params overwrite standard params: {sorted(overlap)}")
        litellm_params.update(extra_litellm_params)

    info: dict = {"mode": mode}
    if supports_function_calling:
        info["supports_function_calling"] = True
    if model_info is not None:
        info.update(model_info)
    return {"model_name": model_name, "litellm_params": litellm_params, "model_info": info}


def _entries(
    models: Iterable[str],
    *,
    name: Callable[[str], str],
    upstream: Callable[[str], str],
    mode: str,
    api_base: str | None = None,
    api_key: str | None = None,
    supports_function_calling: bool = False,
    model_info: Callable[[str], dict[str, int]] | None = None,
    extra_body: Callable[[str], dict | None] | None = None,
) -> list[dict]:
    """Apply the common LiteLLM-entry shape to a roster."""
    return [
        _model_entry(
            name(model),
            upstream(model),
            mode,
            api_base=api_base,
            api_key=api_key,
            supports_function_calling=supports_function_calling,
            model_info=model_info(model) if model_info is not None else None,
            extra_body=extra_body(model) if extra_body is not None else None,
        )
        for model in models
    ]


def _provider_entries(
    models: Iterable[str],
    *,
    provider: Provider,
    upstream_prefix: str,
    protocol: str,
    api_base: str | None = None,
    api_key: str | None = None,
    supports_function_calling: bool = False,
    model_info: Callable[[str], dict[str, int]] | None = None,
) -> list[dict]:
    shape = shape_for(upstream_prefix, protocol)
    return _entries(
        models,
        name=lambda model: exposed_name(provider, shape, model),
        upstream=lambda model: f"{upstream_prefix}/{model}",
        mode=shape_mode(shape),
        api_base=api_base,
        api_key=api_key,
        supports_function_calling=supports_function_calling,
        model_info=model_info,
    )


def _context_extra_body(context: int) -> dict | None:
    return None if context == 128 * 1024 else {"options": {"num_ctx": context}}


def _ollama_variant_entries(
    model: str,
    ollama_model: str,
    contexts: tuple[int, ...],
    *,
    upstream_prefix: str,
    protocol: str,
    api_base: str,
    api_key: str | None,
) -> list[dict]:
    shape = shape_for(upstream_prefix, protocol)
    return [
        _model_entry(
            exposed_name(Provider.OLLAMA, shape, ollama_chat_variant(model, context)),
            f"{upstream_prefix}/{ollama_model}",
            shape_mode(shape),
            api_base=api_base,
            api_key=api_key,
            supports_function_calling=True,
            extra_body=_context_extra_body(context),
        )
        for context in contexts
    ]


def _ollama_entries() -> list[dict]:
    entries: list[dict] = []
    for model, ollama_model, contexts in OLLAMA_CHAT_MODELS:
        entries.extend(
            _ollama_variant_entries(
                model,
                ollama_model,
                contexts,
                upstream_prefix="openai",
                protocol="chat",
                api_base=f"{_OLLAMA_BASE}/v1",
                api_key="ollama",
            )
        )
        entries.extend(
            _ollama_variant_entries(
                model,
                ollama_model,
                contexts,
                upstream_prefix="ollama",
                protocol="chat",
                api_base=_OLLAMA_BASE,
                api_key=None,
            )
        )

    embed_shape = shape_for("ollama", "embed")
    entries.append(
        _model_entry(
            exposed_name(Provider.OLLAMA, embed_shape, OLLAMA_EMBEDDING_MODEL),
            "ollama/qwen3-embedding:4b",
            shape_mode(embed_shape),
            api_base=_OLLAMA_BASE,
        )
    )
    return entries


_CODEX_LIMITS = {model.id: model for model in OPENCLAW_CODEX_MODELS}


def _codex_model_info(model: str) -> dict[str, int]:
    # litellm's own model_cost DB is wrong for these slugs: no entry at all for the
    # anthropic/-prefixed one (advertises null), and the openai/-prefixed twin matches
    # litellm's raw-API entry for gpt-5.6-sol (922k) -- far larger than this
    # subscription path actually serves. Pin the known serving-path limits instead.
    if model not in _CODEX_LIMITS:
        return {}
    limits = _CODEX_LIMITS[model]
    return {
        "max_input_tokens": limits.context_window,
        "max_output_tokens": limits.max_tokens,
        "max_tokens": limits.max_tokens,
    }


def _cliproxy_entries() -> list[dict]:
    entries: list[dict] = []
    for upstream_prefix, protocol, api_base in (
        ("anthropic", "messages", _CLIPROXY_BASE),
        ("openai", "responses", f"{_CLIPROXY_BASE}/v1"),
    ):
        entries.extend(
            _provider_entries(
                CLIPROXY_MODELS,
                provider=Provider.CHATGPT,
                upstream_prefix=upstream_prefix,
                protocol=protocol,
                api_base=api_base,
                api_key="os.environ/CLIPROXY_CLIENT_KEY",
                supports_function_calling=True,
                model_info=_codex_model_info,
            )
        )
    return entries


def _tana_entries() -> list[dict]:
    shape = shape_for(Provider.TANA, "messages")
    return [
        _model_entry(
            exposed_name(Provider.TANA, shape, exposed),
            f"{Provider.TANA}/{Provider.TANA}/{downstream}",
            shape_mode(shape),
            api_base="https://app.tana.inc/functions",
            api_key="os.environ/TANA_FIREBASE_REFRESH_TOKEN",
            timeout=60,
            supports_function_calling=True,
            extra_litellm_params={
                "firebase_api_key": "AIzaSyA9LtJM6Ga9VAwCfj9w_mNORdOaq2yLshQ",
                "tana_user_context": "Generic AI Query",
                "tana_tool_user_context": "Ask Tana",
                "tana_ignore_large_context_warning": True,
                "tana_ignore_out_of_credits_warning": False,
            },
            custom_llm_provider=Provider.TANA,
        )
        for exposed, downstream in TANA_MODELS
    ]


def _antigravity_entries() -> list[dict]:
    return _provider_entries(
        ANTIGRAVITY_MODELS,
        provider=Provider.ANTIGRAVITY,
        upstream_prefix="anthropic",
        protocol="messages",
        api_base=_CLIPROXY_BASE,
        api_key="os.environ/CLIPROXY_CLIENT_KEY",
        supports_function_calling=True,
    )


def _anthropic_entries() -> list[dict]:
    return [
        *_provider_entries(
            ANTHROPIC_MODELS,
            provider=Provider.ANTHROPIC_MAX20,
            upstream_prefix="anthropic",
            protocol="messages",
            api_base=_CLIPROXY_BASE,
            api_key="os.environ/CLIPROXY_CLIENT_KEY",
            supports_function_calling=True,
        ),
        *_provider_entries(
            ANTHROPIC_MODELS,
            provider=Provider.ANTHROPIC_API,
            upstream_prefix="anthropic",
            protocol="messages",
            api_key="os.environ/ANTHROPIC_API_KEY",
            supports_function_calling=True,
        ),
    ]


def _simple_provider_entries() -> list[dict]:
    entries = _provider_entries(
        ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
        provider=Provider.GROQ,
        upstream_prefix="groq",
        protocol="chat",
        api_key="os.environ/GROQ_API_KEY",
        supports_function_calling=True,
    )
    entries.extend(
        _entries(
            ("whisper-large-v3", "whisper-large-v3-turbo"),
            name=lambda model: model,
            upstream=lambda model: f"groq/{model}",
            mode="audio_transcription",
            api_key="os.environ/GROQ_API_KEY",
        )
    )
    entries.extend(
        _provider_entries(
            [model.id for model in GEMINI_MODELS],
            provider=Provider.GOOGLE,
            upstream_prefix="gemini",
            protocol="generate",
            api_key="os.environ/GEMINI_API_KEY",
            supports_function_calling=True,
        )
    )
    entries.extend(
        _provider_entries(
            (GEMINI_EMBEDDING_MODELS[0],),
            provider=Provider.GOOGLE,
            upstream_prefix="gemini",
            protocol="embed",
            api_key="os.environ/GEMINI_API_KEY",
        )
    )
    entries.append(
        _model_entry(
            GEMINI_EMBEDDING_COMPAT_ALIAS,
            f"gemini/{GEMINI_EMBEDDING_MODELS[0]}",
            "embedding",
            api_key="os.environ/GEMINI_API_KEY",
        )
    )
    entries.extend(
        _provider_entries(
            (GEMINI_EMBEDDING_MODELS[1],),
            provider=Provider.GOOGLE,
            upstream_prefix="gemini",
            protocol="embed",
            api_key="os.environ/GEMINI_API_KEY",
        )
    )
    entries.extend(
        _provider_entries(
            MISTRAL_MODELS,
            provider=Provider.MISTRAL,
            upstream_prefix="mistral",
            protocol="chat",
            api_key="os.environ/MISTRAL_API_KEY",
            supports_function_calling=True,
        )
    )
    return entries


def main_proxy_config() -> dict:
    """Return the complete main-proxy config from the shared Python roster."""
    model_list = [
        *_ollama_entries(),
        *_tana_entries(),
        *_cliproxy_entries(),
        *_anthropic_entries(),
        *_antigravity_entries(),
        *_simple_provider_entries(),
    ]
    # The name every consumer uses for this route; the assert catches both a divergence
    # from the shape _cliproxy_entries() derives and "gpt-6-astra" leaving CLIPROXY_MODELS.
    astra_alias_target = codex_responses_name("gpt-6-astra")
    assert astra_alias_target in {entry["model_name"] for entry in model_list}, astra_alias_target
    return {
        "model_list": model_list,
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
            "model_group_alias": {"gpt-6-astra": {"model": astra_alias_target, "hidden": True}}
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
