"""Compose and verify the committed main and ChatGPT-only LiteLLM configs."""

from collections.abc import Iterator

import pytest_bazel
import yaml

from cluster.k8s.litellm.app.model_rosters import ANTHROPIC_MODELS, ZAI_ANTHROPIC_MODELS
from util.bazel.runfiles import get_required_path

_OLLAMA_BASE = "http://ollama.ollama.svc.cluster.local:11434"
_CHATGPT_LITELLM_BASE = "http://litellm-chatgpt.litellm.svc.cluster.local:4000/v1"

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

# ChatGPT subscription via LiteLLM's native `chatgpt/` provider (ChatGPT backend-api /
# Codex OAuth). The stateful provider runs only in the single-replica litellm-chatgpt
# Deployment; the main proxy chains these public model names to that internal proxy via
# LiteLLM's native Responses passthrough. A broken OAuth token can therefore take down
# ChatGPT models without blocking startup of every other provider.
#
# No upstream API key: auth is a flat auth.json (access_token/refresh_token/id_token,
# with expires_at/account_id auto-derived) on a writable PVC, seeded from the
# litellm-chatgpt-auth-seed secret. The provider refreshes the access token on demand and
# rewrites that file, so the mount must be read-write. `drop_params` strips the
# max_tokens/metadata fields this backend rejects.
#
# Only these models are served by the Codex/ChatGPT-account backend (verified live).
# Others tried and rejected with "not supported when using Codex with a ChatGPT
# account": gpt-5.4-pro, gpt-5.3-codex, gpt-5.3-instant, gpt-5.3-chat-latest.
#
# GOTCHA: usable via STREAMING only. Non-streaming responses come back with an empty
# output[] and the /v1/chat/completions bridge fails with "Unknown items in responses
# API response: []" — an unfixed LiteLLM bug (BerriAI/litellm#25429; fix PRs like #27562
# still unmerged as of litellm 1.90.x). Callers must send stream:true to /v1/responses.
_CHATGPT_MODELS: list[str] = [
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.3-codex-spark",
]


# Groq (fast open-model inference: Llama chat + Whisper ASR). Key from the
# GROQ_API_KEY env var (litellm-groq-key secret). Free tier.
GROQ_CHAT_MODELS: list[str] = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
GROQ_WHISPER_MODELS: list[str] = ["whisper-large-v3", "whisper-large-v3-turbo"]

# Google AI (Gemini). Key from the GEMINI_API_KEY env var (litellm-gemini-key
# secret). Chat lineup verified against generativelanguage.googleapis.com/v1beta/models
# (2026-07-18): the Gemini-3.x preview family (pro / flash / flash-lite) + gemini-3.5-flash,
# the stable 2.5 pair, and the -latest aliases that auto-point at the newest generation.
# Remaining specialty SKUs (image, tts, live/bidi, customtools, imagen, veo) are
# intentionally excluded — add on demand.
GEMINI_MODELS: list[str] = [
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-pro-latest",
    "gemini-flash-latest",
]

# Gemini embeddings, same key as the chat lineup. Added for OpenClaw memory search,
# whose index needs an embedding backend and had none — see
# plans/personal_agents/findings/harness_behaviour.md F9.
#
# Both are stable and their embedding spaces are **mutually incompatible**: vectors
# from one cannot be compared against the other, so switching a consumer between
# them means re-embedding its whole corpus. Verified against
# ai.google.dev/gemini-api/docs/embeddings (2026-07-30).
#
# gemini-embedding-2 is the current one (8,192 input tokens, multimodal);
# gemini-embedding-001 is text-only with a 2,048-token limit and is kept because it
# is the longer-established route through LiteLLM. Both expose flexible output
# dimensionality (128-3072, recommended 768/1536/3072), selected per request rather
# than per deployment, so neither entry pins a size.
GEMINI_EMBEDDING_MODELS: list[str] = ["gemini-embedding-2", "gemini-embedding-001"]


# Tana (Tana-UI models) fronted through the DB-less tana-litellm proxy. tana-litellm is a
# standard LiteLLM that speaks /v1/messages and authenticates with the same litellm-master-key
# the main proxy already holds, so we chain to it with the `anthropic/` provider — a verbatim
# /v1/messages passthrough with no shape translation (same pattern as z.ai GLM above). Key
# stays in-cluster only; laptop consumers use a scoped tana virtual key against this proxy.
#
# Tana encodes reasoning effort in the model name (`/medium`, `/high`), not a `reasoning_effort`
# param, so there is no clean "one model + effort knob" to map onto. We expose one model per
# family at its default effort. Each entry: (exposed model_name, tana-litellm downstream
# model_name). The downstream name's slash stays inside the `anthropic/` arg, never exposed.
_TANA_LITELLM_BASE = "http://tana-litellm.litellm.svc.cluster.local:4000"
_TANA_MODELS: list[tuple[str, str]] = [
    ("tana-claude-sonnet-4-6", "claude-sonnet-4-6/medium"),
    ("tana-claude-opus-4-6", "claude-opus-4-6/high"),
    ("tana-claude-haiku-4-5", "claude-haiku-4-5-20251001"),
]


# CLIProxyAPI (ChatGPT/Codex subscription) fronted with the `anthropic/` provider. It speaks
# Anthropic /v1/messages and is the one path that translates Codex tool calls correctly
# (function_call -> tool_use) — the thing LiteLLM's own Responses bridge and the `*-chatgpt`
# entries above could not do. Client key from CLIPROXY_CLIENT_KEY (cli-proxy-api-client-key
# mirrored into the litellm namespace). Reasoning effort is driven by Claude Code's
# effortLevel -> reasoning_effort passthrough, so one entry per slug suffices.
_CLIPROXY_BASE = "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317"
_CLIPROXY_MODELS: list[str] = [
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.3-codex-spark",
]


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


def _gemini_entries() -> Iterator[dict]:
    for model in GEMINI_MODELS:
        yield {
            "model_name": model,
            "litellm_params": {"model": f"gemini/{model}", "api_key": "os.environ/GEMINI_API_KEY"},
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }
    for model in GEMINI_EMBEDDING_MODELS:
        yield {
            "model_name": model,
            "litellm_params": {"model": f"gemini/{model}", "api_key": "os.environ/GEMINI_API_KEY"},
            "model_info": {"mode": "embedding"},
        }


def _chatgpt_proxy_entries() -> Iterator[dict]:
    for model in _CHATGPT_MODELS:
        model_name = f"{model}-chatgpt"
        yield {
            "model_name": model_name,
            "litellm_params": {
                "model": f"litellm_proxy/{model_name}",
                # The Responses passthrough appends /responses, so this base includes /v1.
                "api_base": _CHATGPT_LITELLM_BASE,
                "api_key": "os.environ/LITELLM_MASTER_KEY",
            },
            "model_info": {"mode": "responses"},
        }


def _chatgpt_provider_entries() -> Iterator[dict]:
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


def _tana_entries() -> Iterator[dict]:
    for exposed, downstream in _TANA_MODELS:
        yield {
            "model_name": exposed,
            "litellm_params": {
                "model": f"anthropic/{downstream}",
                "api_base": _TANA_LITELLM_BASE,
                "api_key": "os.environ/LITELLM_MASTER_KEY",
            },
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def _cliproxy_entries() -> Iterator[dict]:
    for model in _CLIPROXY_MODELS:
        yield {
            "model_name": f"codex-{model}",
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
    model_list.extend(_zai_anthropic_entries())
    model_list.extend(_tana_entries())
    model_list.extend(_cliproxy_entries())
    model_list.extend(_chatgpt_proxy_entries())
    model_list.extend(_anthropic_entries())
    model_list.extend(_groq_entries())
    model_list.extend(_gemini_entries())

    # Master key and Langfuse credentials are injected as env vars in the
    # Deployment; not repeated here.
    return {
        "model_list": model_list,
        "litellm_settings": {"drop_params": True, "callbacks": ["langfuse_otel", "prometheus"]},
        # The virtual-key DB (DATABASE_URL in the Deployment) must never become a
        # second model source: models stay in this committed, parity-tested config.
        # DB-registered models also bypass the chatgpt provider's responses config
        # and break streaming (BerriAI/litellm#28044).
        "general_settings": {"store_model_in_db": False},
    }


def _expected_chatgpt_config() -> dict:
    return {
        "model_list": list(_chatgpt_provider_entries()),
        "litellm_settings": {"drop_params": True, "callbacks": ["prometheus"]},
        "general_settings": {"store_model_in_db": False},
    }


def _load_config(filename: str) -> dict:
    loaded = yaml.safe_load(get_required_path(f"ducktape/cluster/k8s/litellm/app/{filename}").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_committed_configs_match_composed_expectations() -> None:
    expected = {"proxy-config.yaml": _expected_main_config(), "chatgpt-proxy-config.yaml": _expected_chatgpt_config()}
    for filename, expected_config in expected.items():
        assert _load_config(filename) == expected_config, filename


def test_chatgpt_models_are_isolated_behind_internal_proxy() -> None:
    main_config = _expected_main_config()
    chatgpt_config = _expected_chatgpt_config()
    main_models = {
        model["model_name"]: model for model in main_config["model_list"] if model["model_name"].endswith("-chatgpt")
    }
    chatgpt_models = {model["model_name"]: model for model in chatgpt_config["model_list"]}

    assert chatgpt_models
    assert main_models.keys() == chatgpt_models.keys()
    assert all(not model["litellm_params"]["model"].startswith("chatgpt/") for model in main_config["model_list"])
    for name, main_model in main_models.items():
        assert main_model == {
            "model_name": name,
            "litellm_params": {
                "model": f"litellm_proxy/{name}",
                "api_base": "http://litellm-chatgpt.litellm.svc.cluster.local:4000/v1",
                "api_key": "os.environ/LITELLM_MASTER_KEY",
            },
            "model_info": {"mode": "responses"},
        }
        assert chatgpt_models[name] == {
            "model_name": name,
            "litellm_params": {"model": f"chatgpt/{name.removesuffix('-chatgpt')}"},
            "model_info": {"mode": "responses"},
        }


def test_chatgpt_auth_state_is_absent_from_main_deployment() -> None:
    main = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/deployment.yaml").read_text())
    chatgpt = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/chatgpt-deployment.yaml").read_text())
    main_pod = main["spec"]["template"]["spec"]
    chatgpt_pod = chatgpt["spec"]["template"]["spec"]

    assert main["spec"]["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
    # Suspended 2026-08-06 (superseded by CLIProxyAPI); 1 while it ran, because the
    # rotating auth.json tolerates exactly one writer. The rest of this test still
    # holds: the point is that auth state lives here and NOT in the main deployment,
    # which stays true at rest and must keep holding until the whole thing is deleted.
    assert chatgpt["spec"]["replicas"] == 0
    assert chatgpt["spec"]["strategy"] == {"type": "Recreate"}
    assert "initContainers" not in main_pod
    assert [container["name"] for container in chatgpt_pod["initContainers"]] == ["seed-chatgpt-auth"]
    assert chatgpt_pod["initContainers"][0]["command"][-1] == (
        "grep -q '\"refresh_token\"' /data/chatgpt/auth.json || cp /seed/auth.json /data/chatgpt/auth.json"
    )
    assert "CHATGPT_TOKEN_DIR" not in {env["name"] for env in main_pod["containers"][0]["env"]}
    assert "CHATGPT_TOKEN_DIR" in {env["name"] for env in chatgpt_pod["containers"][0]["env"]}
    assert {volume["name"] for volume in main_pod["volumes"]}.isdisjoint({"chatgpt-auth", "chatgpt-auth-seed"})
    chatgpt_volumes = {volume["name"]: volume for volume in chatgpt_pod["volumes"]}
    assert chatgpt_volumes.keys() == {"config", "chatgpt-auth", "chatgpt-auth-seed"}
    assert chatgpt_volumes["config"]["configMap"]["name"] == "litellm-chatgpt-config"
    assert chatgpt_volumes["chatgpt-auth"]["persistentVolumeClaim"]["claimName"] == "litellm-chatgpt-auth"
    assert chatgpt_volumes["chatgpt-auth-seed"]["secret"]["secretName"] == "litellm-chatgpt-auth-seed"
    assert main["spec"]["selector"] != chatgpt["spec"]["selector"]


def test_config_maps_mount_their_matching_committed_configs() -> None:
    kustomization = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/kustomization.yaml").read_text())
    config_files = {config["name"]: config["files"] for config in kustomization["configMapGenerator"]}
    assert config_files == {
        "litellm-chatgpt-config": ["config.yaml=chatgpt-proxy-config.yaml"],
        "litellm-config": ["config.yaml=proxy-config.yaml"],
    }


def test_chatgpt_proxy_is_cluster_private_and_selectors_are_disjoint() -> None:
    main_service = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/service.yaml").read_text())
    chatgpt_service = yaml.safe_load(
        get_required_path("ducktape/cluster/k8s/litellm/app/chatgpt-service.yaml").read_text()
    )
    route = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/httproute.yaml").read_text())

    assert main_service["spec"]["type"] == "ClusterIP"
    assert chatgpt_service["spec"]["type"] == "ClusterIP"
    assert main_service["spec"]["selector"] == {"app.kubernetes.io/name": "litellm"}
    assert chatgpt_service["spec"]["selector"] == {"app.kubernetes.io/name": "litellm-chatgpt"}
    assert {backend["name"] for rule in route["spec"]["rules"] for backend in rule["backendRefs"]} == {"litellm"}


if __name__ == "__main__":
    pytest_bazel.main()
