"""Build the LiteLLM configuration payloads from the shared model rosters."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from cluster.k8s.litellm.app.model_rosters import (
    ANTHROPIC_MODELS,
    ASTRA_CONTEXT_WINDOW,
    ASTRA_MAX_TOKENS,
    CLIPROXY_MODELS,
    CODEX_CONTEXT_WINDOW,
    CODEX_MAX_TOKENS,
    CODEX_MEASURED_MODELS,
    GEMINI_EMBEDDING_MODELS,
    GEMINI_MODELS,
    MISTRAL_MODELS,
    TANA_MODELS,
    Provider,
    exposed_name,
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
    supports_function_calling: bool = False,
    model_info: dict[str, int] | None = None,
    extra_body: dict | None = None,
    custom_llm_provider: str | None = None,
) -> dict:
    """Build one LiteLLM model entry while omitting unset optional fields."""
    litellm_params: dict = {"model": model}
    if api_base is not None:
        litellm_params["api_base"] = api_base
    if api_key is not None:
        litellm_params["api_key"] = api_key
    if extra_body is not None:
        litellm_params["extra_body"] = extra_body
    if custom_llm_provider is not None:
        litellm_params["custom_llm_provider"] = custom_llm_provider

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
    suffixes: list[tuple[str, int]],
    *,
    upstream_prefix: str,
    protocol: str,
    api_base: str,
    api_key: str | None,
) -> list[dict]:
    shape = shape_for(upstream_prefix, protocol)
    return [
        _model_entry(
            exposed_name(Provider.OLLAMA, shape, f"{model}-{suffix}"),
            f"{upstream_prefix}/{ollama_model}",
            shape_mode(shape),
            api_base=api_base,
            api_key=api_key,
            supports_function_calling=True,
            extra_body=_context_extra_body(context),
        )
        for suffix, context in suffixes
    ]


def _ollama_entries() -> list[dict]:
    entries: list[dict] = []
    for model, ollama_model, contexts in (
        ("gpt-oss-20b", "gpt-oss:20b", (128 * 1024, 256 * 1024, 512 * 1024, 1024 * 1024)),
        ("gpt-oss-120b", "gpt-oss:120b", (128 * 1024,)),
        ("gemma4-31b-it-q8_0", "gemma4:31b-it-q8_0", (128 * 1024,)),
    ):
        suffixes = [(f"{context // 1024}k" if context < 1024 * 1024 else "1m", context) for context in contexts]
        entries.extend(
            _ollama_variant_entries(
                model,
                ollama_model,
                suffixes,
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
                suffixes,
                upstream_prefix="ollama",
                protocol="chat",
                api_base=_OLLAMA_BASE,
                api_key=None,
            )
        )

    embed_shape = shape_for("ollama", "embed")
    entries.append(
        _model_entry(
            exposed_name(Provider.OLLAMA, embed_shape, "qwen3-embedding-4b"),
            "ollama/qwen3-embedding:4b",
            shape_mode(embed_shape),
            api_base=_OLLAMA_BASE,
        )
    )
    return entries


def _codex_model_info(model: str) -> dict[str, int]:
    if model == "gpt-6-astra":
        return {
            "max_input_tokens": ASTRA_CONTEXT_WINDOW,
            "max_output_tokens": ASTRA_MAX_TOKENS,
            "max_tokens": ASTRA_MAX_TOKENS,
        }
    if model in CODEX_MEASURED_MODELS:
        # litellm's own model_cost DB is wrong for these slugs: no entry at all for the
        # anthropic/-prefixed one (advertises null), and the openai/-prefixed twin matches
        # litellm's raw-API entry for gpt-5.6-sol (922k) -- far larger than this
        # subscription path actually serves. Pin the measured CLIProxyAPI window instead.
        return {
            "max_input_tokens": CODEX_CONTEXT_WINDOW,
            "max_output_tokens": CODEX_MAX_TOKENS,
            "max_tokens": CODEX_MAX_TOKENS,
        }
    return {}


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
    shape = shape_for("tana", "messages")
    return [
        _model_entry(
            exposed_name(Provider.TANA, shape, exposed),
            f"tana/tana/{downstream}",
            shape_mode(shape),
            supports_function_calling=True,
            custom_llm_provider="tana",
        )
        for exposed, downstream in TANA_MODELS
    ]


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
            GEMINI_MODELS,
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
    # Compatibility alias for public-coder-agent's durable OpenClaw index. Keep until
    # that index is deliberately rebuilt under the prefixed name.
    entries.append(
        _model_entry(
            "gemini-embedding-2", "gemini/gemini-embedding-2", "embedding", api_key="os.environ/GEMINI_API_KEY"
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
        *_simple_provider_entries(),
    ]
    # Reuses the exact name _cliproxy_entries() gives this model (same provider, same
    # shape_for(upstream_prefix, protocol) derivation), so the alias can't drift from
    # what's actually served by construction; the one thing that can't be derived this
    # way is whether "gpt-6-astra" still exists in CLIPROXY_MODELS at all.
    astra_alias_target = exposed_name(Provider.CHATGPT, shape_for("openai", "responses"), "gpt-6-astra")
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
