"""The public-coder-agent OpenClaw configuration rendered into its ConfigMap."""

from __future__ import annotations

from cluster.cdk8s.openclaw_gateway import disabled_commands, session_memory_hook, trusted_proxy_gateway
from cluster.cdk8s.public_coder.egress import PROXY_URL as _EGRESS_PROXY
from model_catalog.catalog import (
    ANTIGRAVITY_FLASH_3,
    ANTIGRAVITY_FLASH_36,
    ANTIGRAVITY_FLASH_37,
    ANTIGRAVITY_FLASH_38,
    ANTIGRAVITY_FLASH_LITE_31,
    ANTIGRAVITY_GPT_OSS_120B_MEDIUM,
    ANTIGRAVITY_OPUS,
    ANTIGRAVITY_PRO,
    ANTIGRAVITY_PRO_LOW,
    ANTIGRAVITY_SONNET,
    GEMINI_ROUTES,
    GPT6_ASTRA_RESPONSES,
    GPT6_LUNA_RESPONSES,
    GPT6_SOL_RESPONSES,
    OLLAMA_EMBEDDING_ROUTE,
    Provider,
    Route,
)


def _model_entry(route: Route, *, context_budget: int, output_budget: int) -> dict:
    model = route.model
    if model.reasoning is None:
        raise ValueError(f"missing OpenClaw metadata for {route.id}")
    account_name = {
        Provider.CHATGPT: "Codex subscription",
        Provider.GOOGLE: "Google AI",
        Provider.ANTIGRAVITY: "Google Antigravity",
    }[route.upstream.provider]
    return {
        "contextWindow": context_budget,
        "id": route.id,
        "input": ["text", "image"],
        "maxTokens": output_budget,
        "name": f"{route.display_name} ({account_name} via LiteLLM)",
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
                "model": {"primary": f"litellm/{GPT6_LUNA_RESPONSES.id}"},
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
                    "model": {"primary": f"litellm/{GPT6_ASTRA_RESPONSES.id}"},
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
                "model": OLLAMA_EMBEDDING_ROUTE.id,
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
                # placeholder from the environment, while Agentplane egress swaps the real
                # password only in Matrix's login body. The resulting access token is
                # cached in the state PVC.
                "enabled": True,
                "homeserver": "https://matrix.allegedly.works",
                # The Matrix plugin's guarded fetch reads this field explicitly; generic
                # HTTP(S)_PROXY is not enough.
                "proxy": _EGRESS_PROXY,
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
                    # Preserve the paused client's existing selection, order and budgets.
                    # These are OpenClaw settings, not provider capacity claims;
                    # adding provider metadata must not opt another route in.
                    "models": [
                        _model_entry(GPT6_ASTRA_RESPONSES, context_budget=872_000, output_budget=128_000),
                        *(
                            _model_entry(route, context_budget=372_000, output_budget=128_000)
                            for route in (GPT6_LUNA_RESPONSES, GPT6_SOL_RESPONSES)
                        ),
                        *(
                            _model_entry(route, context_budget=1_048_576, output_budget=65_536)
                            for route in GEMINI_ROUTES
                        ),
                        *(
                            _model_entry(route, context_budget=200_000, output_budget=64_000)
                            for route in (ANTIGRAVITY_OPUS, ANTIGRAVITY_SONNET)
                        ),
                        *(
                            _model_entry(route, context_budget=1_048_576, output_budget=65_536)
                            for route in (
                                ANTIGRAVITY_FLASH_36,
                                ANTIGRAVITY_FLASH_37,
                                ANTIGRAVITY_FLASH_38,
                                ANTIGRAVITY_FLASH_3,
                            )
                        ),
                        *(
                            _model_entry(route, context_budget=1_048_576, output_budget=65_535)
                            for route in (ANTIGRAVITY_PRO, ANTIGRAVITY_PRO_LOW)
                        ),
                        _model_entry(ANTIGRAVITY_GPT_OSS_120B_MEDIUM, context_budget=114_000, output_budget=32_768),
                        _model_entry(ANTIGRAVITY_FLASH_LITE_31, context_budget=1_048_576, output_budget=65_535),
                    ],
                    "request": {"allowPrivateNetwork": True},
                }
            }
        },
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
                            # placeholder, which Agentplane egress replaces at Brave's API endpoint.
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
