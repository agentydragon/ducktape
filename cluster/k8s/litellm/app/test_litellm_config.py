"""Compose and verify the committed main LiteLLM config."""

from collections.abc import Callable, Iterator
from functools import partial

import pytest_bazel
import yaml
from more_itertools import one

from cluster.k8s.litellm.app.model_rosters import (
    ANTHROPIC_MODELS,
    CLIPROXY_MODELS,
    GEMINI_EMBEDDING_MODELS,
    GEMINI_MODELS,
    TANA_MODELS,
    ApiShape,
    Provider,
    exposed_name,
    legacy_google_name,
    legacy_messages_name,
    legacy_responses_name,
    legacy_tana_name,
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
# since the private litellm-chatgpt Deployment was retired 2026-08-06 (cluster/docs/plan.md).
# One pod, exposed twice below because its two clients speak different wire protocols and
# each one's native surface must be reached without a LiteLLM-side translation:
#
#   Messages wire   -> `anthropic/` provider -> CLIProxyAPI /v1/messages  (Claude Code)
#   Responses wire  -> `openai/` provider    -> CLIProxyAPI /v1/responses (Codex CLI)
#
# Client key from CLIPROXY_CLIENT_KEY (cli-proxy-api-client-key mirrored into the litellm
# namespace). Reasoning effort rides on the request (Claude Code's effortLevel, Codex's
# `reasoning.effort`), never a model-slug suffix, so one entry per slug per surface.
# Each wire surface is served under its legacy and its #4823 name (model_rosters.py).
_CLIPROXY_BASE = "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317"


# Real Anthropic API (plain claude-* names — the "-anthropic" suffix means
# "Anthropic SHAPE via z.ai", not this). Key: litellm-anthropic-key, an ESO
# mirror of the spend-capped haku-cloud workspace key. Access control is per-key
# model allowlists because the main LiteLLM service is cluster-reachable.
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


def _gemini_chat_entries(name: Callable[[str], str]) -> Iterator[dict]:
    for model in GEMINI_MODELS:
        yield {
            "model_name": name(model),
            "litellm_params": {"model": f"gemini/{model}", "api_key": "os.environ/GEMINI_API_KEY"},
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def _gemini_embedding_entries(name: Callable[[str], str]) -> Iterator[dict]:
    for model in GEMINI_EMBEDDING_MODELS:
        yield {
            "model_name": name(model),
            "litellm_params": {"model": f"gemini/{model}", "api_key": "os.environ/GEMINI_API_KEY"},
            "model_info": {"mode": "embedding"},
        }


def _tana_entries(name: Callable[[str], str]) -> Iterator[dict]:
    for base, downstream in TANA_MODELS:
        yield {
            "model_name": name(base),
            "litellm_params": {
                "model": f"anthropic/{downstream}",
                "api_base": _TANA_LITELLM_BASE,
                "api_key": "os.environ/LITELLM_MASTER_KEY",
            },
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


# Anthropic Messages surface: the one path that translates Codex tool calls correctly
# (function_call -> tool_use) for Claude Code — what LiteLLM's own Responses bridge could
# not do (BerriAI/litellm#25429).
def _cliproxy_messages_entries(name: Callable[[str], str]) -> Iterator[dict]:
    for model in CLIPROXY_MODELS:
        yield {
            "model_name": name(model),
            "litellm_params": {
                "model": f"anthropic/{model}",
                "api_base": _CLIPROXY_BASE,
                "api_key": "os.environ/CLIPROXY_CLIENT_KEY",
            },
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


# OpenAI Responses surface, for Codex CLI (`wire_api = "responses"` in its config).
# `openai/` is what makes this a passthrough: LiteLLM serves /v1/responses natively for
# the OpenAI provider and forwards to `{api_base}/responses`, whereas every other provider
# — including the `anthropic/` entries above — gets a bridge that rewrites the request into
# chat completions. api_base therefore carries the `/v1` the `anthropic/` entries omit.
def _cliproxy_responses_entries(name: Callable[[str], str]) -> Iterator[dict]:
    for model in CLIPROXY_MODELS:
        yield {
            "model_name": name(model),
            "litellm_params": {
                "model": f"openai/{model}",
                "api_base": f"{_CLIPROXY_BASE}/v1",
                "api_key": "os.environ/CLIPROXY_CLIENT_KEY",
            },
            "model_info": {"mode": "responses", "supports_function_calling": True},
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
    model_list.extend(_tana_entries(legacy_tana_name))
    model_list.extend(_tana_entries(partial(exposed_name, Provider.TANA, ApiShape.ANT_MESSAGES)))
    model_list.extend(_cliproxy_messages_entries(legacy_messages_name))
    model_list.extend(_cliproxy_messages_entries(partial(exposed_name, Provider.CHATGPT, ApiShape.ANT_MESSAGES)))
    model_list.extend(_cliproxy_responses_entries(legacy_responses_name))
    model_list.extend(_cliproxy_responses_entries(partial(exposed_name, Provider.CHATGPT, ApiShape.OAI_RESPONSES)))
    model_list.extend(_anthropic_entries())
    model_list.extend(_groq_entries())
    model_list.extend(_gemini_chat_entries(legacy_google_name))
    model_list.extend(_gemini_embedding_entries(legacy_google_name))
    model_list.extend(_gemini_chat_entries(partial(exposed_name, Provider.GOOGLE, ApiShape.OAI_CHAT)))
    model_list.extend(_gemini_embedding_entries(partial(exposed_name, Provider.GOOGLE, ApiShape.OAI_EMBEDDINGS)))

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
    assert tf_locals["oai_lane_models"] == [legacy_responses_name(model) for model in CLIPROXY_MODELS] + [
        exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model) for model in CLIPROXY_MODELS
    ]
    assert tf_locals["codex_client_models"] == [legacy_messages_name(model) for model in CLIPROXY_MODELS] + [
        exposed_name(Provider.CHATGPT, ApiShape.ANT_MESSAGES, model) for model in CLIPROXY_MODELS
    ]


# main.tf's own comment: "Model names must match generated model_name entries in
# cluster/k8s/litellm/app/proxy-config.yaml". These are the remaining live-key
# locals that spell names out literally, so every element must resolve.
_TF_LITERAL_MODEL_LOCALS = [
    "oai_lane_models",
    "tana_client_models",
    "codex_client_models",
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
# hardcodes wire_api="responses" (haku/runtime/x/bridge/codex_options.py), so the model
# must be a Responses-wire entry (either era's name); a Messages-wire name fails every
# turn at /v1/responses
# (haku/console/x/codex_app_server/testdata/real_provider_failure.sanitized.jsonl).
def test_console_codex_chat_runtimes_use_oai_responses_wire_models() -> None:
    config = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/config.yaml").read_text())
    responses_wire_names = {legacy_responses_name(model) for model in CLIPROXY_MODELS} | {
        exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model) for model in CLIPROXY_MODELS
    }
    for name, runtime in config["chat_runtimes"].items():
        implementation = runtime["implementation"]
        if implementation["kind"] == "codex_app_server":
            assert implementation["model"] in responses_wire_names, name


def test_config_maps_mount_their_matching_committed_configs() -> None:
    kustomization = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/kustomization.yaml").read_text())
    config_files = {config["name"]: config["files"] for config in kustomization["configMapGenerator"]}
    assert config_files == {"litellm-config": ["config.yaml=proxy-config.yaml"]}


if __name__ == "__main__":
    pytest_bazel.main()
