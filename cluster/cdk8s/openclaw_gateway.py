"""Shared OpenClaw gateway/config fragments used by every agent's openclaw.json[5]
generator (haku_openclaw_spike_config.py, public_coder_agent_config.py) -- all of them
sit behind the same Authentik trusted-proxy outpost and reach the same Haku Console MCP
server, differing only in per-agent device-approval scopes, control-UI hostname, and
Haku request timeout.
"""

from __future__ import annotations


def disabled_commands() -> dict:
    """config/mcp/restart in-gateway commands, off on every agent -- these are configured
    declaratively from this repo, not interactively from the gateway's own command surface.
    """
    return {"config": False, "mcp": False, "restart": False}


def session_memory_hook() -> dict:
    """The internal session-memory hook, enabled with the same window on every agent."""
    return {"internal": {"entries": {"session-memory": {"enabled": True, "llmSlug": False, "messages": 15}}}}


def trusted_proxy_gateway(*, allowed_origin: str, device_approve_scopes: list[str]) -> dict:
    """The gateway.auth trusted-proxy pattern shared by every agent behind the Authentik
    outpost. Browser requests arrive with Authentik identity headers. What makes this
    safe at all: app/networkpolicy-ingress.yaml admits only the outpost's pods, so
    nothing else can forge x-authentik-username -- and allowUsers below further
    restricts it to agentydragon alone.

    device_approve_scopes is a ceiling, not a grant: it caps what an auto-approved
    device may request, defaulting to operator.{read,write,approvals,questions} when
    unset. Authentik has already authenticated the browser, so this does not
    additionally demand a manual device pairing on top of it -- 2026.8.1 retired
    gateway.controlUi.dangerouslyDisableDeviceAuth silently (so the Control UI started
    asking to pair); this is its replacement, and unlike the old flag it caps what an
    auto-approved device may do.
    """
    return {
        "auth": {
            "mode": "trusted-proxy",
            "trustedProxy": {
                "allowLoopback": True,
                "allowUsers": ["agentydragon"],
                "requiredHeaders": ["x-authentik-email", "x-forwarded-host", "x-forwarded-proto"],
                "userHeader": "x-authentik-username",
                "deviceAutoApprove": {"enabled": True, "scopes": device_approve_scopes},
            },
        },
        # Authentik reaches the pod over the cluster network, hence "lan" rather than
        # loopback. The ingress NetworkPolicy, not this bind address, is the trust
        # boundary. Valid values are loopback, lan, tailnet, auto, custom.
        "bind": "lan",
        "controlUi": {"allowedOrigins": [allowed_origin]},
        "mode": "local",
        "trustedProxies": ["10.0.0.0/8"],
    }


def haku_console_mcp(*, request_timeout_ms: int) -> dict:
    """The mcp.servers entry for Haku Console, reachable from every agent's gateway.
    request_timeout_ms should stay above the caller's expected worst-case Haku round
    trip -- Haku exposes a large tools catalog and can take longer than the default
    client timeout, so setting this too low means the client times out before Haku's
    own terminal-result wait does.
    """
    return {
        "servers": {
            "haku-console": {
                "url": "https://haku.allegedly.works/mcp",
                "transport": "streamable-http",
                # Haku fans out to many upstream MCP servers and each call is
                # independent, so let the agent issue concurrent tool calls against it.
                "supportsParallelToolCalls": True,
                "requestTimeoutMs": request_timeout_ms,
                "headers": {
                    # Haku's token is an inert placeholder here; runtime interpolation
                    # and iron-proxy substitution provide the controller-owned secret.
                    "Authorization": "Bearer ${HAKU_CONSOLE_TOKEN}"
                },
            }
        }
    }
