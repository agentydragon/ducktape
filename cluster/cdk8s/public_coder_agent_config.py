"""Generates public-coder-agent/app/openclaw.json5's content -- see model_rosters.py for
the model-name scheme and the Codex/Gemini rosters this pulls from.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s.config_format import json5_config
from cluster.cdk8s.generation import config_map_chart, write_charts
from cluster.cdk8s.model_rosters import (
    GEMINI_CONTEXT_WINDOW,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MODELS,
    OLLAMA_EMBEDDING_MODEL,
    OPENCLAW_CODEX_MODELS,
    ApiShape,
    CodexModel,
    GeminiModel,
    Provider,
    codex_responses_name,
    exposed_name,
)
from cluster.cdk8s.openclaw_gateway import (
    disabled_commands,
    haku_console_mcp,
    session_memory_hook,
    trusted_proxy_gateway,
)

_CODEX_BY_ID = {model.id: model for model in OPENCLAW_CODEX_MODELS}
_DEFAULT_CODEX_MODEL = _CODEX_BY_ID["gpt-5.6-luna"]
_TPM_CODEX_MODEL = _CODEX_BY_ID["gpt-6-astra"]


def _litellm_model_id(model: CodexModel) -> str:
    return f"litellm/{codex_responses_name(model.id)}"


def _codex_model_entry(model: CodexModel) -> dict:
    return {
        "contextWindow": model.context_window,
        "id": codex_responses_name(model.id),
        "input": ["text", "image"],
        "maxTokens": model.max_tokens,
        "name": f"{model.display_name} (Codex subscription via LiteLLM)",
        "reasoning": True,
    }


def _gemini_model_entry(model: GeminiModel) -> dict:
    return {
        "contextWindow": GEMINI_CONTEXT_WINDOW,
        "id": exposed_name(Provider.GOOGLE, ApiShape.GOOG_GENERATE, model.id),
        "input": ["text", "image"],
        "maxTokens": GEMINI_MAX_OUTPUT_TOKENS,
        "name": f"{model.display_name} (Google AI via LiteLLM)",
        "reasoning": model.reasoning,
    }


def config() -> dict:
    return {
        # This file is the GitOps source of truth. The app init container copies it
        # into the state PVC; OPENCLAW_CONFIG_PATH points OpenClaw back at that copy.
        "agents": {
            "ownership": "explicit",
            "defaults": {
                "userTimezone": "America/Los_Angeles",
                # The working path is Codex subscription -> CLIProxyAPI -> LiteLLM's
                # native Responses endpoint. Keep this on the measured 5.6 roster.
                "model": {"primary": _litellm_model_id(_DEFAULT_CODEX_MODEL)},
                "sandbox": {"mode": "off"},
                "maxConcurrent": 16,
                "subagents": {"maxConcurrent": 16, "maxChildrenPerAgent": 16},
                "skills": [],
                "verboseDefault": "full",
            },
            "entries": {
                "coder": {"name": "Coder"},
                "haku_console_tpm": {
                    "name": "Haku Console TPM",
                    "model": {"primary": _litellm_model_id(_TPM_CODEX_MODEL)},
                },
            },
        },
        # Route the Matrix account to the public coder agent explicitly; multi-agent
        # configurations do not infer a channel owner.
        "bindings": [{"agentId": "coder", "match": {"channel": "matrix"}}],
        # Memory uses LiteLLM's OpenAI-compatible embeddings route; keep it out
        # of the public egress proxy because LiteLLM is an in-cluster Service.
        "memory": {
            "search": {
                "enabled": True,
                "sources": ["memory"],
                "provider": "openai-compatible",
                # This is a new embedding identity; changing it deliberately requires a
                # full rebuild of the durable index after the rollout.
                "model": exposed_name(Provider.OLLAMA, ApiShape.OLM_EMBED, OLLAMA_EMBEDDING_MODEL),
                "remote": {
                    "baseUrl": "http://litellm.litellm.svc.cluster.local:4000/v1",
                    "apiKey": "${OPENCLAW_LITELLM_API_KEY}",
                },
            }
        },
        "commands": disabled_commands(),
        "channels": {
            "matrix": {
                # MATRIX_PASSWORD is intentionally absent: OpenClaw reads the stable
                # placeholder from the environment, while iron-proxy swaps the real
                # password only in Matrix's login body. The resulting access token is
                # cached in the state PVC.
                "enabled": True,
                "homeserver": "https://matrix.allegedly.works",
                # The Matrix plugin's guarded fetch reads this field explicitly; generic
                # HTTP(S)_PROXY is not enough. Keep it equal to Deployment HTTPS_PROXY:
                # the same proxy also performs Matrix login-password substitution.
                "proxy": "http://public-coder-agent-proxy.public-coder-agent.svc.cluster.local:8080",
                "userId": "@public-coder-agent:allegedly.works",
                "dm": {
                    "policy": "allowlist",
                    "allowFrom": ["@agentydragon:allegedly.works"],
                    # Keep each DM room as an independent conversation.
                    "sessionScope": "per-room",
                },
                # Group rooms are intentionally not accepted.
                "groupPolicy": "disabled",
                # Matrix DMs arrive as invites, so classify them only after joining.
                "autoJoin": "always",
                # Preserve Matrix's threaded reply presentation.
                "threadReplies": "always",
            }
        },
        "cron": {"enabled": True, "triggers": {"enabled": True}},
        # Local backend and subagent calls have no Authentik headers, so they use
        # the generated gateway password supplied by the Deployment. Keep it out
        # of this file.
        "gateway": trusted_proxy_gateway(
            allowed_origin="https://public-coder-agent.allegedly.works",
            device_approve_scopes=[
                "operator.read",
                "operator.write",
                "operator.approvals",
                "operator.questions",
                "operator.admin",
            ],
        ),
        "hooks": session_memory_hook(),
        "models": {
            "providers": {
                # One provider intentionally covers both working model families. Codex
                # uses openai-responses and chatgpt/oai-responses/* through CLIProxyAPI;
                # Gemini uses the same LiteLLM Responses surface. No Anthropic provider.
                "litellm": {
                    "agentRuntime": {"id": "openclaw"},
                    "api": "openai-responses",
                    "apiKey": "${OPENCLAW_LITELLM_API_KEY}",
                    "baseUrl": "http://litellm.litellm.svc.cluster.local:4000/v1",
                    "models": [
                        *(_codex_model_entry(model) for model in OPENCLAW_CODEX_MODELS),
                        *(_gemini_model_entry(model) for model in GEMINI_MODELS),
                    ],
                    "request": {"allowPrivateNetwork": True},
                }
            }
        },
        "mcp": haku_console_mcp(request_timeout_ms=70000),
        "plugins": {
            "entries": {
                # Matrix is bundled into the image as a trusted plugin; loading a copy
                # through plugins.load.paths breaks its state-storage trust requirement.
                "matrix": {"enabled": True},
                "brave": {
                    "enabled": True,
                    "config": {
                        "webSearch": {
                            # Brave is bundled and image-pinned; the pod sees only a
                            # placeholder, which iron-proxy replaces at Brave's API endpoint.
                            "apiKey": {"source": "env", "id": "BRAVE_API_KEY"},
                            "mode": "web",
                        }
                    },
                },
                # Disabled on this headless agent: companion-device plugins need a paired
                # phone/desktop, and Ollama is not used. No phone-control entry: this
                # OpenClaw build doesn't register that plugin, so configuring it is a
                # stale no-op the gateway flags on every startup.
                "canvas": {"enabled": False},
                "device-pair": {"enabled": False},
                "file-transfer": {"enabled": False},
                "talk-voice": {"enabled": False},
                "ollama": {"enabled": False},
            }
        },
        "tools": {
            # Cross-agent sessions_send/sessions_spawn targets are invisible under
            # the default "tree" scope. OpenClaw 2026.8.1 configures visibility only
            # at this global scope; agentToAgent below remains the exact agent gate.
            "sessions": {"visibility": "all"},
            # Off by default upstream: lets "haku_console_tpm" hand tasks to "coder"
            # via sessions_send/sessions_spawn. Both ids must be listed for either
            # direction of that handoff to be admitted.
            "agentToAgent": {"enabled": True, "allow": ["haku_console_tpm", "coder"]},
            "web": {
                "search": {
                    "enabled": True,
                    "provider": "brave",
                    "maxResults": 5,
                    "timeoutSeconds": 30,
                    "cacheTtlMinutes": 15,
                }
            },
        },
    }


def chart(app: App) -> Chart:
    return config_map_chart(
        app,
        chart_name="public-coder-agent-config",
        configmap_name="public-coder-agent-config",
        namespace="public-coder-agent",
        data={"openclaw.json5": json5_config(config())},
    )


def write_manifests(root: Path) -> None:
    write_charts(root, "cluster/k8s/agents/public-coder-agent/app", chart)
