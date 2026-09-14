"""Generate the LiteLLM configuration manifests with Python cdk8s.

The model roster is deliberately ordinary Python: it can be imported by tests and
by the other configuration consumers, while cdk8s supplies both the application
YAML serialization and the final Kubernetes ConfigMap envelope. The generated YAML
is still a normal Flux/Kustomize input; this experiment does not give CI cluster
credentials or ask cdk8s to apply anything.
"""

import argparse
from pathlib import Path

from cdk8s import App, Chart, Yaml
from cdk8s_plus_33 import ConfigMap

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
    ZAI_ANTHROPIC_MODELS,
    ApiShape,
    Provider,
    exposed_name,
)
from tana.litellm_proxy.model_registry import TANA_LLM_PROXY_RESPONDING_MODELS

_OLLAMA_BASE = "http://ollama.ollama.svc.cluster.local:11434"
_CLIPROXY_BASE = "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317"
_TANA_CUSTOM_HANDLER = """\
from __future__ import annotations

from tana.litellm_proxy.custom_handler import tana_handler

__all__ = ["tana_handler"]
"""


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
) -> dict:
    """Build one LiteLLM model entry while omitting unset optional fields."""
    litellm_params: dict = {"model": model}
    if api_base is not None:
        litellm_params["api_base"] = api_base
    if api_key is not None:
        litellm_params["api_key"] = api_key
    if extra_body is not None:
        litellm_params["extra_body"] = extra_body

    info: dict = {"mode": mode}
    if supports_function_calling:
        info["supports_function_calling"] = True
    if model_info is not None:
        info.update(model_info)
    return {"model_name": model_name, "litellm_params": litellm_params, "model_info": info}


def _ollama_entries() -> list[dict]:
    entries: list[dict] = []
    for model, ollama_model, contexts in (
        ("gpt-oss-20b", "gpt-oss:20b", (128 * 1024, 256 * 1024, 512 * 1024, 1024 * 1024)),
        ("gpt-oss-120b", "gpt-oss:120b", (128 * 1024,)),
        ("gemma4-31b-it-q8_0", "gemma4:31b-it-q8_0", (128 * 1024,)),
    ):
        suffixes = [(f"{context // 1024}k" if context < 1024 * 1024 else "1m", context) for context in contexts]
        for suffix, context in suffixes:
            extra_body = None if context == 128 * 1024 else {"options": {"num_ctx": context}}
            entries.append(
                _model_entry(
                    exposed_name(Provider.OLLAMA, ApiShape.OAI_CHAT, f"{model}-{suffix}"),
                    f"openai/{ollama_model}",
                    "chat",
                    api_base=f"{_OLLAMA_BASE}/v1",
                    api_key="ollama",
                    supports_function_calling=True,
                    extra_body=extra_body,
                )
            )
        for suffix, context in suffixes:
            extra_body = None if context == 128 * 1024 else {"options": {"num_ctx": context}}
            entries.append(
                _model_entry(
                    exposed_name(Provider.OLLAMA, ApiShape.OLM_CHAT, f"{model}-{suffix}"),
                    f"ollama/{ollama_model}",
                    "chat",
                    api_base=_OLLAMA_BASE,
                    supports_function_calling=True,
                    extra_body=extra_body,
                )
            )

    entries.append(
        _model_entry(
            exposed_name(Provider.OLLAMA, ApiShape.OLM_EMBED, "qwen3-embedding-4b"),
            "ollama/qwen3-embedding:4b",
            "embedding",
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
        return {
            "max_input_tokens": CODEX_CONTEXT_WINDOW,
            "max_output_tokens": CODEX_MAX_TOKENS,
            "max_tokens": CODEX_MAX_TOKENS,
        }
    return {}


def _cliproxy_entries() -> list[dict]:
    return [
        _model_entry(
            exposed_name(Provider.CHATGPT, shape, model),
            f"{provider_model}/{model}",
            mode,
            api_base=api_base,
            api_key="os.environ/CLIPROXY_CLIENT_KEY",
            supports_function_calling=True,
            model_info=_codex_model_info(model),
        )
        for shape, provider_model, api_base, mode in (
            (ApiShape.ANT_MESSAGES, "anthropic", _CLIPROXY_BASE, "chat"),
            (ApiShape.OAI_RESPONSES, "openai", f"{_CLIPROXY_BASE}/v1", "responses"),
        )
        for model in CLIPROXY_MODELS
    ]


def _tana_entries() -> list[dict]:
    return [
        _model_entry(
            exposed_name(Provider.TANA, ApiShape.ANT_MESSAGES, exposed),
            f"anthropic/{downstream}",
            "chat",
            api_base="http://tana-litellm.litellm.svc.cluster.local:4000",
            api_key="os.environ/LITELLM_MASTER_KEY",
            supports_function_calling=True,
        )
        for exposed, downstream in TANA_MODELS
    ]


def _anthropic_entries() -> list[dict]:
    return [
        _model_entry(
            exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model),
            f"anthropic/{model}",
            "chat",
            api_base=_CLIPROXY_BASE,
            api_key="os.environ/CLIPROXY_CLIENT_KEY",
            supports_function_calling=True,
        )
        for model in ANTHROPIC_MODELS
    ] + [
        _model_entry(
            exposed_name(Provider.ANTHROPIC_API, ApiShape.ANT_MESSAGES, model),
            f"anthropic/{model}",
            "chat",
            api_key="os.environ/ANTHROPIC_API_KEY",
            supports_function_calling=True,
        )
        for model in ANTHROPIC_MODELS
    ]


def _simple_provider_entries() -> list[dict]:
    entries: list[dict] = [
        _model_entry(
            exposed_name(Provider.GROQ, ApiShape.OAI_CHAT, model),
            f"groq/{model}",
            "chat",
            api_key="os.environ/GROQ_API_KEY",
            supports_function_calling=True,
        )
        for model in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant")
    ]
    entries.extend(
        _model_entry(model, f"groq/{model}", "audio_transcription", api_key="os.environ/GROQ_API_KEY")
        for model in ("whisper-large-v3", "whisper-large-v3-turbo")
    )
    entries.extend(
        _model_entry(
            exposed_name(Provider.GOOGLE, ApiShape.GOOG_GENERATE, model),
            f"gemini/{model}",
            "chat",
            api_key="os.environ/GEMINI_API_KEY",
            supports_function_calling=True,
        )
        for model in GEMINI_MODELS
    )
    entries.append(
        _model_entry(
            exposed_name(Provider.GOOGLE, ApiShape.GOOG_EMBED, GEMINI_EMBEDDING_MODELS[0]),
            f"gemini/{GEMINI_EMBEDDING_MODELS[0]}",
            "embedding",
            api_key="os.environ/GEMINI_API_KEY",
        )
    )
    # This unprefixed alias is part of the durable OpenClaw embedding index's
    # identity. It is intentionally retained until that index is rebuilt.
    entries.append(
        _model_entry(
            "gemini-embedding-2", "gemini/gemini-embedding-2", "embedding", api_key="os.environ/GEMINI_API_KEY"
        )
    )
    entries.append(
        _model_entry(
            exposed_name(Provider.GOOGLE, ApiShape.GOOG_EMBED, GEMINI_EMBEDDING_MODELS[1]),
            f"gemini/{GEMINI_EMBEDDING_MODELS[1]}",
            "embedding",
            api_key="os.environ/GEMINI_API_KEY",
        )
    )
    entries.extend(
        _model_entry(
            exposed_name(Provider.MISTRAL, ApiShape.OAI_CHAT, model),
            f"mistral/{model}",
            "chat",
            api_key="os.environ/MISTRAL_API_KEY",
            supports_function_calling=True,
        )
        for model in MISTRAL_MODELS
    )
    return entries


def main_proxy_config() -> dict:
    """Return the complete main-proxy config from the shared Python roster."""
    return {
        "model_list": [
            *_ollama_entries(),
            *_tana_entries(),
            *_cliproxy_entries(),
            *_anthropic_entries(),
            *_simple_provider_entries(),
        ],
        "litellm_settings": {"drop_params": True, "callbacks": ["langfuse_otel", "prometheus"]},
        "router_settings": {
            "model_group_alias": {
                "gpt-6-astra": {
                    "model": exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, "gpt-6-astra"),
                    "hidden": True,
                }
            }
        },
        "general_settings": {"forward_client_headers_to_llm_api": True, "store_model_in_db": False},
    }


def _yaml_config(config: dict) -> str:
    # cdk8s owns serialization of the application YAML as well as the
    # surrounding Kubernetes object. This is intentionally not PyYAML: the
    # app config is data, while cdk8s is the one manifest/YAML writer here.
    return Yaml.format_objects([config])


def _formatted_config_map_data(data: dict[str, object]) -> dict[str, str]:
    formatted: dict[str, str] = {}
    for filename, value in data.items():
        if filename == "config.yaml":
            assert isinstance(value, dict)
            formatted[filename] = _yaml_config(value)
        else:
            assert isinstance(value, str)
            formatted[filename] = value
    return formatted


def _tana_proxy_config() -> dict:
    return {
        "model_list": [
            {
                "model_name": model.model_id,
                "litellm_params": {"model": f"tana/tana/{model.model_id}", "custom_llm_provider": "tana"},
                "model_info": {"mode": "chat", "supports_function_calling": True},
            }
            for model in TANA_LLM_PROXY_RESPONDING_MODELS
        ],
        "litellm_settings": {
            "drop_params": True,
            "callbacks": ["langfuse_otel"],
            "custom_provider_map": [{"provider": "tana", "custom_handler": "custom_handler.tana_handler"}],
        },
    }


def _workers_proxy_config() -> dict:
    return {
        "model_list": [
            {
                "model_name": f"{model}-anthropic",
                "litellm_params": {
                    "model": f"litellm_proxy/{model}-anthropic",
                    "api_base": "http://litellm.litellm.svc.cluster.local:4000",
                    "api_key": "os.environ/ZAI_ZONE_KEY",
                },
            }
            for model in ZAI_ANTHROPIC_MODELS
        ],
        "litellm_settings": {"drop_params": True, "callbacks": ["prometheus"]},
        "general_settings": {"store_model_in_db": False},
    }


def _config_maps() -> tuple[tuple[str, str, str, dict[str, object]], ...]:
    return (
        ("litellm", "litellm-config", "litellm", {"config.yaml": main_proxy_config()}),
        (
            "tana-litellm",
            "tana-litellm-config",
            "litellm",
            {"config.yaml": _tana_proxy_config(), "custom_handler.py": _TANA_CUSTOM_HANDLER},
        ),
        ("workers-litellm", "workers-litellm-config", "haku-dispatch", {"config.yaml": _workers_proxy_config()}),
    )


def generate_manifests(output_directory: Path) -> None:
    """Synthesize ordinary Kubernetes ConfigMaps into output_directory."""
    output_directory.mkdir(parents=True, exist_ok=True)

    app = App(outdir=str(output_directory))
    for chart_name, config_map_name, namespace, data in _config_maps():
        chart = Chart(app, chart_name, disable_resource_name_hashes=True)
        ConfigMap(
            chart,
            "config",
            metadata={
                "name": config_map_name,
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": "cdk8s", "app.kubernetes.io/part-of": "litellm"},
                "annotations": {
                    "ducktape.dev/generated": "by cdk8s under Bazel",
                    "ducktape.dev/delivery": "Flux can consume this ordinary Kubernetes YAML",
                },
            },
            data=_formatted_config_map_data(data),
        )
    app.synth()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    generate_manifests(args.output_dir.resolve())


if __name__ == "__main__":
    main()
