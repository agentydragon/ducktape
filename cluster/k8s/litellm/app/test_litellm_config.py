"""Compose and verify the committed main LiteLLM config."""

from collections.abc import Iterator

import pytest_bazel
import yaml
from more_itertools import one

from cluster.k8s.litellm.app.model_rosters import (
    ANTHROPIC_MODELS,
    CLIPROXY_MODELS,
    CODEX_CONTEXT_WINDOW,
    CODEX_MAX_TOKENS,
    CODEX_MEASURED_MODELS,
    GEMINI_EMBEDDING_MODELS,
    GEMINI_MODELS,
    TANA_MODELS,
    ApiShape,
    Provider,
    exposed_name,
)
from cluster.validation.terraform_hcl import locals_blocks
from util.bazel.runfiles import get_required_path

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

# Groq (fast open-model inference: Llama chat + Whisper ASR). Key from the
# GROQ_API_KEY env var (litellm-groq-key secret). Free tier.
GROQ_CHAT_MODELS: list[str] = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
GROQ_WHISPER_MODELS: list[str] = ["whisper-large-v3", "whisper-large-v3-turbo"]

# Tana (Tana-UI models, TANA_MODELS in model_rosters.py) fronted through the DB-less
# tana-litellm proxy. tana-litellm is a standard LiteLLM that speaks /v1/messages and
# authenticates with the same litellm-master-key the main proxy already holds, so we
# chain to it with the `anthropic/` provider — a verbatim /v1/messages passthrough with
# no shape translation. Key stays in-cluster only; laptop consumers use a scoped tana
# virtual key against this proxy.
_TANA_LITELLM_BASE = "http://tana-litellm.litellm.svc.cluster.local:4000"


# CLIProxyAPI (ChatGPT/Codex subscription) is the only route to the Codex subscription
# since the private litellm-chatgpt Deployment was retired 2026-08-06 (cluster/docs/decisions.md).
# One pod, exposed twice below because its two clients speak different wire protocols and
# each one's native surface must be reached without a LiteLLM-side translation:
#
#   Messages wire   -> `anthropic/` provider -> CLIProxyAPI /v1/messages  (Claude Code)
#   Responses wire  -> `openai/` provider    -> CLIProxyAPI /v1/responses (Codex CLI)
#
# Client key from CLIPROXY_CLIENT_KEY (cli-proxy-api-client-key mirrored into the litellm
# namespace). Reasoning effort rides on the request (Claude Code's effortLevel, Codex's
# `reasoning.effort`), never a model-slug suffix, so one entry per slug per surface.
# Each wire surface is served under its #4823 scheme name (model_rosters.py).
_CLIPROXY_BASE = "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317"


# Real Anthropic API (`anthropic-api/ant-messages/*` names — the "-anthropic" suffix means
# "Anthropic SHAPE via z.ai", not this). Key: litellm-anthropic-key, an ESO
# mirror of the spend-capped haku-cloud workspace key. Access control is per-key
# model allowlists because the main LiteLLM service is cluster-reachable.
def _anthropic_entries() -> Iterator[dict]:
    for model in ANTHROPIC_MODELS:
        yield {
            "model_name": exposed_name(Provider.ANTHROPIC_API, ApiShape.ANT_MESSAGES, model),
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


def _gemini_chat_entries() -> Iterator[dict]:
    for model in GEMINI_MODELS:
        yield {
            "model_name": exposed_name(Provider.GOOGLE, ApiShape.OAI_CHAT, model),
            "litellm_params": {"model": f"gemini/{model}", "api_key": "os.environ/GEMINI_API_KEY"},
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def _gemini_embedding_entries() -> Iterator[dict]:
    for model in GEMINI_EMBEDDING_MODELS:
        yield {
            "model_name": exposed_name(Provider.GOOGLE, ApiShape.OAI_EMBEDDINGS, model),
            "litellm_params": {"model": f"gemini/{model}", "api_key": "os.environ/GEMINI_API_KEY"},
            "model_info": {"mode": "embedding"},
        }


def _tana_entries() -> Iterator[dict]:
    for base, downstream in TANA_MODELS:
        yield {
            "model_name": exposed_name(Provider.TANA, ApiShape.ANT_MESSAGES, base),
            "litellm_params": {
                "model": f"anthropic/{downstream}",
                "api_base": _TANA_LITELLM_BASE,
                "api_key": "os.environ/LITELLM_MASTER_KEY",
            },
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


# model_info token limits for the measured 5.6 models; empty for the rest. litellm's
# model_cost DB has no window for the anthropic/-prefixed slugs and mis-values the
# openai/-prefixed twins (~1.05M/922K raw-API), so the probed routes pin the measured
# CODEX_CONTEXT_WINDOW and the unprobed models are left for litellm to answer.
def _codex_window(model: str) -> dict[str, int]:
    if model not in CODEX_MEASURED_MODELS:
        return {}
    return {
        "max_input_tokens": CODEX_CONTEXT_WINDOW,
        "max_output_tokens": CODEX_MAX_TOKENS,
        "max_tokens": CODEX_MAX_TOKENS,
    }


# Anthropic Messages surface: the one path that translates Codex tool calls correctly
# (function_call -> tool_use) for Claude Code — what LiteLLM's own Responses bridge could
# not do (BerriAI/litellm#25429).
def _cliproxy_messages_entries() -> Iterator[dict]:
    for model in CLIPROXY_MODELS:
        yield {
            "model_name": exposed_name(Provider.CHATGPT, ApiShape.ANT_MESSAGES, model),
            "litellm_params": {
                "model": f"anthropic/{model}",
                "api_base": _CLIPROXY_BASE,
                "api_key": "os.environ/CLIPROXY_CLIENT_KEY",
            },
            "model_info": {"mode": "chat", "supports_function_calling": True} | _codex_window(model),
        }


# OpenAI Responses surface, for Codex CLI and OpenClaw (`wire_api = "responses"` in
# their configs).
# `openai/` is what makes this a passthrough: LiteLLM serves /v1/responses natively for
# the OpenAI provider and forwards to `{api_base}/responses`, whereas every other provider
# — including the `anthropic/` entries above — gets a bridge that rewrites the request into
# chat completions. api_base therefore carries the `/v1` the `anthropic/` entries omit.
def _cliproxy_responses_entries() -> Iterator[dict]:
    for model in CLIPROXY_MODELS:
        yield {
            "model_name": exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model),
            "litellm_params": {
                "model": f"openai/{model}",
                "api_base": f"{_CLIPROXY_BASE}/v1",
                "api_key": "os.environ/CLIPROXY_CLIENT_KEY",
            },
            "model_info": {"mode": "responses", "supports_function_calling": True} | _codex_window(model),
        }


# Claude Code-subscription models on CLIProxyAPI's Claude OAuth session — the same
# `anthropic/` /v1/messages passthrough as the chatgpt/ant-messages entries above, a
# different upstream OAuth session on the same pod. Messages wire only: Claude Code has no
# Responses-wire twin. Exposes ANTHROPIC_MODELS: the subscription and the direct API serve
# the same current generation, so they share one roster. The exposed prefix is
# `anthropic-max20/ant-messages/`; the consuming key allowlist is
# `claude_client_models` in tf/gitops/litellm-keys/main.tf, pinned to this roster by
# test_terraform_claude_allowlist_matches_the_anthropic_model_list below.
def _cliproxy_claude_entries() -> Iterator[dict]:
    for model in ANTHROPIC_MODELS:
        yield {
            "model_name": exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model),
            "litellm_params": {
                "model": f"anthropic/{model}",
                "api_base": _CLIPROXY_BASE,
                "api_key": "os.environ/CLIPROXY_CLIENT_KEY",
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


def _expected_main_config() -> dict:
    model_list: list[dict] = []
    for tag, ctx_variants in _MODELS:
        model_list.extend(_model_entries(tag, ctx_variants))
    model_list.extend(_tana_entries())
    model_list.extend(_cliproxy_messages_entries())
    model_list.extend(_cliproxy_responses_entries())
    model_list.extend(_cliproxy_claude_entries())
    model_list.extend(_anthropic_entries())
    model_list.extend(_groq_entries())
    model_list.extend(_gemini_chat_entries())
    model_list.extend(_gemini_embedding_entries())

    # Master key and Langfuse credentials are injected as env vars in the
    # Deployment; not repeated here.
    return {
        "model_list": model_list,
        "litellm_settings": {"drop_params": True, "callbacks": ["langfuse_otel", "prometheus"]},
        # The virtual-key DB (DATABASE_URL in the Deployment) must never become a
        # second model source: models stay in this committed, parity-tested config.
        # DB-registered models also bypass per-model responses config and break
        # streaming (BerriAI/litellm#28044).
        "general_settings": {"store_model_in_db": False},
    }


def _load_config(filename: str) -> dict:
    loaded = yaml.safe_load(get_required_path(f"ducktape/cluster/k8s/litellm/app/{filename}").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_committed_configs_match_composed_expectations() -> None:
    expected = {"proxy-config.yaml": _expected_main_config()}
    for filename, expected_config in expected.items():
        assert _load_config(filename) == expected_config, filename


# The Codex-subscription names appear in three places: CLIPROXY_MODELS (model_rosters.py),
# and `oai_lane_models` + `codex_client_models` in tf/gitops/litellm-keys/main.tf, which
# scope the virtual keys. A comment in that file asks the lists to be kept in sync; these
# pin it instead, so adding a Codex model cannot half-land and leave a key allowlisting a
# model that does not exist (or omitting one that does).
def _litellm_keys_locals() -> dict:
    blocks = locals_blocks(get_required_path("ducktape/tf/gitops/litellm-keys/main.tf"))
    return one(blocks)


def test_terraform_codex_allowlists_match_the_cliproxy_model_list() -> None:
    tf_locals = _litellm_keys_locals()
    assert tf_locals["oai_lane_models"] == [
        exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model) for model in CLIPROXY_MODELS
    ]
    assert tf_locals["codex_client_models"] == [
        exposed_name(Provider.CHATGPT, ApiShape.ANT_MESSAGES, model) for model in CLIPROXY_MODELS
    ]


# The Claude-subscription names live in ANTHROPIC_MODELS (model_rosters.py) and `claude_client_models`
# in tf/gitops/litellm-keys/main.tf, which scopes the haku-console-claude runner key. This pins the
# two in sync, so a new Claude model cannot half-land and leave the key allowlisting one the proxy
# does not serve (or omitting one it does).
def test_terraform_claude_allowlist_matches_the_anthropic_model_list() -> None:
    tf_locals = _litellm_keys_locals()
    assert tf_locals["claude_client_models"] == [
        exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model) for model in ANTHROPIC_MODELS
    ]


# main.tf's own comment: "Model names must match generated model_name entries in
# cluster/k8s/litellm/app/proxy-config.yaml". These are the remaining live-key
# locals that spell names out literally, so every element must resolve.
_TF_LITERAL_MODEL_LOCALS = [
    "oai_lane_models",
    "tana_client_models",
    "codex_client_models",
    "claude_client_models",
    "embedding_client_models",
    "gemini_client_models",
]


def test_terraform_key_allowlists_only_name_models_the_proxy_serves() -> None:
    served = {entry["model_name"] for entry in _expected_main_config()["model_list"]}
    tf_locals = _litellm_keys_locals()
    for local in _TF_LITERAL_MODEL_LOCALS:
        missing = [model for model in tf_locals[local] if model not in served]
        assert not missing, f"{local} allows models the proxy does not serve: {missing}"


# haku-console picks its Codex chat runtime's model from Git YAML — the one Codex consumer
# whose model choice lives outside the baked-config and Terraform pins above. The runner
# hardcodes wire_api="responses" (haku/runner/codex/options.py), so the model
# must be a Responses-wire entry; a Messages-wire name fails every turn at /v1/responses
# (haku/console/x/codex_app_server/testdata/real_provider_failure.sanitized.jsonl).
def test_console_codex_harnesses_use_oai_responses_wire_models() -> None:
    config = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/config.yaml").read_text())
    responses_wire_names = {exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model) for model in CLIPROXY_MODELS}
    for name, runtime in config["harnesses"].items():
        implementation = runtime["implementation"]
        if implementation["kind"] == "codex_app_server":
            assert implementation["model"] in responses_wire_names, name


# haku-console's Claude runtime (#4670) picks model + haiku_model from Git YAML, the same
# outside-the-Terraform-pins spot as the Codex runtime above. Claude Code speaks the Anthropic
# Messages wire against CLIProxyAPI's Claude subscription, so both must be
# anthropic-max20/ant-messages/*
# entries the proxy serves -- never the codex chatgpt/ant-messages/* lane a stale GPT guess would
# name, which the haku-console-claude key does not admit and which is a broken model turn every
# request. Admits any served claude-lane model, so it pins the wire without hardcoding the choice.
def test_console_claude_harness_uses_claude_ant_messages_wire_models() -> None:
    config = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/config.yaml").read_text())
    claude_wire_names = {
        exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model) for model in ANTHROPIC_MODELS
    }
    for name, runtime in config["harnesses"].items():
        implementation = runtime["implementation"]
        if implementation["kind"] == "claude_code":
            assert implementation["model"] in claude_wire_names, name
            assert implementation["haiku_model"] in claude_wire_names, name


def test_config_maps_mount_their_matching_committed_configs() -> None:
    kustomization = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/kustomization.yaml").read_text())
    config_files = {config["name"]: config["files"] for config in kustomization["configMapGenerator"]}
    assert config_files == {"litellm-config": ["config.yaml=proxy-config.yaml"]}


if __name__ == "__main__":
    pytest_bazel.main()
