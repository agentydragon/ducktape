"""Generates cluster/k8s/litellm/app/proxy-config.yaml (raw LiteLLM proxy config).

Run to regenerate the committed file:
    bazel run //cluster/k8s/litellm/app:generate_litellm_bin

Parity enforced by:
    bazel test //cluster/k8s/litellm/app:test_generate_litellm

# TODO: Consider alternatives that render at apply time, eliminating this
# generator + parity test entirely:
#
# - Local Helm chart via kustomize `helmCharts`: write a tiny chart with a
#   Go template that ranges over a model list in values.yaml. Flux's
#   kustomize-controller renders it on reconcile — no committed output.
#   Natural fit with the existing kustomize/Flux stack.
#
# - Timoni: CUE-based module rendered by the Flux Timoni controller.
#   Stronger typing than Go templates; same "no committed output" property.
"""

from collections.abc import Iterator

import yaml

from devinfra.prettier import prettier_format_in_place
from util.bazel.workspace import get_build_workspace_directory

_OLLAMA_BASE = "http://ollama.ollama.svc.cluster.local:11434"

# (name suffix, num_ctx). None num_ctx = model default (128k for gpt-oss).
_CTX_VARIANTS: list[tuple[str, int | None]] = [("128k", None), ("256k", 262_144), ("512k", 524_288), ("1m", 1_048_576)]

# (ollama model tag, context variants to expose)
_MODELS: list[tuple[str, list[tuple[str, int | None]]]] = [
    ("gpt-oss:20b", _CTX_VARIANTS),
    ("gpt-oss:120b", [("128k", None)]),
    # gemma4 trains at 128k; expose only that ctx variant.
    ("gemma4:31b-it-q8_0", [("128k", None)]),
]

# z.ai (GLM) served via z.ai's Anthropic Messages endpoint. The key comes from the
# ZAI_API_KEY env var (litellm-zai-key secret). Routing GLM through the Anthropic shape
# avoids the union-tool-input bug GLM hits on the OpenAI chat shape (z.ai's Anthropic
# adapter parses GLM's XML tool calls back into proper JSON `tool_use.input` objects). See
# docs/zai_api.md. LiteLLM fronts it so it logs to Langfuse (via `litellm_metadata`) and
# props need not hold ZAI_API_KEY. Exposed via LiteLLM's `/v1/messages` endpoint; props'
# llm-proxy routes its /v1/messages here. The OpenAI-shaped coding-plan route was removed
# in favor of this one.
_ZAI_ANTHROPIC_BASE = "https://api.z.ai/api/anthropic"
# Full GLM matrix z.ai serves on the Anthropic endpoint (per /api/anthropic/v1/models, 2026-06).
ZAI_ANTHROPIC_MODELS: list[str] = [
    "glm-4.5",
    "glm-4.5-air",
    "glm-4.6",
    "glm-4.7",
    "glm-5",
    "glm-5-turbo",
    "glm-5.1",
    "glm-5.2",
]


# ChatGPT subscription via LiteLLM's native `chatgpt/` provider (ChatGPT backend-api /
# Codex OAuth). No API key: auth is a flat auth.json (access_token/refresh_token/id_token,
# with expires_at/account_id auto-derived) on a writable PVC, seeded from the
# litellm-chatgpt-auth-seed secret; see deployment.yaml. The provider refreshes the access
# token on demand and rewrites that file, so the mount must be read-write. `drop_params`
# (below) strips the max_tokens/metadata fields this backend rejects.
#
# Only these three are served by the Codex/ChatGPT-account backend (verified live).
# Others tried and rejected with "not supported when using Codex with a ChatGPT
# account": gpt-5.4-pro, gpt-5.3-codex, gpt-5.3-instant, gpt-5.3-chat-latest.
#
# GOTCHA: usable via STREAMING only. Non-streaming responses come back with an empty
# output[] and the /v1/chat/completions bridge fails with "Unknown items in responses
# API response: []" — an unfixed LiteLLM bug (BerriAI/litellm#25429; fix PRs like #27562
# still unmerged as of litellm 1.90.x). Callers must send stream:true to /v1/responses.
_CHATGPT_MODELS: list[str] = ["gpt-5.4", "gpt-5.5", "gpt-5.3-codex-spark"]


# Real Anthropic API (plain claude-* names — the "-anthropic" suffix above means
# "Anthropic SHAPE via z.ai", not this). Key: litellm-anthropic-key, an ESO
# mirror of the spend-capped haku-cloud workspace key. Consumers: the haku
# dispatcher's classifier virtual key (tf/gitops/litellm-keys) — main LiteLLM is
# cluster-reachable, so access control is per-key model allowlists.
ANTHROPIC_MODELS: list[str] = ["claude-sonnet-5", "claude-haiku-4-5"]


# Groq (fast open-model inference: Llama chat + Whisper ASR). Key from the
# GROQ_API_KEY env var (litellm-groq-key secret). Free tier.
GROQ_CHAT_MODELS: list[str] = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
GROQ_WHISPER_MODELS: list[str] = ["whisper-large-v3", "whisper-large-v3-turbo"]

# TODO(gemini-litellm): wire Gemini in once the key is recovered. The only copy is
# cluster/k8s/agents/openclaw/gateway-secrets/gemini-api-key.sops.yaml (encrypted to
# admin+cluster-secrets), and openclaw was never deployed — no openclaw-gateway namespace
# and no live in-cluster secret to read (verified: 0 gemini/AIza hits across 521 secrets).
# Recover the plaintext (decrypt with the admin age key, or re-issue a key), then add a
# litellm-level sops Secret litellm-gemini-key + GEMINI_API_KEY env in deployment.yaml +
# GEMINI_MODELS here (e.g. gemini-2.5-pro, gemini-2.5-flash) and regenerate proxy-config.yaml.
# Same pattern as Groq above; see memory reference_litellm_provider_keys.


def _anthropic_entries() -> Iterator[dict]:
    for model in ANTHROPIC_MODELS:
        yield {
            "model_name": model,
            "litellm_params": {"model": f"anthropic/{model}", "api_key": "os.environ/ANTHROPIC_API_KEY"},
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def _groq_entries() -> Iterator[dict]:
    for model in GROQ_CHAT_MODELS:
        yield {
            "model_name": model,
            "litellm_params": {"model": f"groq/{model}", "api_key": "os.environ/GROQ_API_KEY"},
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }
    for model in GROQ_WHISPER_MODELS:
        yield {
            "model_name": model,
            "litellm_params": {"model": f"groq/{model}", "api_key": "os.environ/GROQ_API_KEY"},
            "model_info": {"mode": "audio_transcription"},
        }


def _chatgpt_entries() -> Iterator[dict]:
    for model in _CHATGPT_MODELS:
        yield {
            "model_name": f"{model}-chatgpt",
            "litellm_params": {"model": f"chatgpt/{model}"},
            "model_info": {"mode": "responses"},
        }


def _zai_anthropic_entries() -> Iterator[dict]:
    for model in ZAI_ANTHROPIC_MODELS:
        yield {
            "model_name": f"{model}-anthropic",
            "litellm_params": {
                # `anthropic/` provider → LiteLLM posts Anthropic Messages to
                # {api_base}/v1/messages with x-api-key, no shape translation.
                "model": f"anthropic/{model}",
                "api_base": _ZAI_ANTHROPIC_BASE,
                "api_key": "os.environ/ZAI_API_KEY",
            },
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def _model_entries(tag: str, ctx_variants: list[tuple[str, int | None]]) -> Iterator[dict]:
    name_base = tag.replace(":", "-")
    for api, suffix, api_base in [
        ("openai", "-openai-chat", f"{_OLLAMA_BASE}/v1"),
        ("ollama", "-ollama-native", _OLLAMA_BASE),
    ]:
        for ctx_suffix, num_ctx in ctx_variants:
            params: dict = {"model": f"{api}/{tag}", "api_base": api_base}
            # Ollama doesn't require an API key, but the OpenAI SDK (used by
            # LiteLLM's openai provider) refuses to initialize without one.
            # TODO: Use a real API key once Ollama per-user auth is deployed
            # (see cluster/docs/plan.md "Ollama: per-user auth").
            if api == "openai":
                params["api_key"] = "ollama"
            if num_ctx is not None:
                params["extra_body"] = {"options": {"num_ctx": num_ctx}}
            yield {
                "model_name": f"{name_base}-{ctx_suffix}{suffix}",
                "litellm_params": params,
                "model_info": {"mode": "chat", "supports_function_calling": True},
            }


def generate() -> str:
    model_list: list[dict] = []
    for tag, ctx_variants in _MODELS:
        model_list.extend(_model_entries(tag, ctx_variants))
    model_list.extend(_zai_anthropic_entries())
    model_list.extend(_chatgpt_entries())
    model_list.extend(_anthropic_entries())
    model_list.extend(_groq_entries())

    # Master key and Langfuse credentials are injected as env vars in the
    # Deployment; not repeated here.
    proxy_config = {
        "model_list": model_list,
        "litellm_settings": {"drop_params": True, "callbacks": ["langfuse_otel", "prometheus"]},
        # The virtual-key DB (DATABASE_URL in the Deployment) must never become a
        # second model source: models stay in this generated, parity-tested config.
        # DB-registered models also bypass the chatgpt provider's responses config
        # and break streaming (BerriAI/litellm#28044).
        "general_settings": {"store_model_in_db": False},
    }

    header = "# Generated by //cluster/k8s/litellm/app:generate_litellm_bin — do not edit by hand.\n"
    return header + yaml.dump(proxy_config, default_flow_style=False, sort_keys=False, allow_unicode=True)


def main() -> None:
    out_path = get_build_workspace_directory() / "cluster/k8s/litellm/app/proxy-config.yaml"
    out_path.write_text(generate())
    prettier_format_in_place(out_path)
    print(f"Generated {out_path}")


if __name__ == "__main__":
    main()
