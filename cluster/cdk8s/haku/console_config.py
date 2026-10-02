"""The console's non-secret deploy catalog (`haku.console.mcp_config.ConsoleConfigFile`),
rendered into the `config` ConfigMap. Secret leaves stay out of it: the
Deployment overlays them from Secrets through `HAKU_CONSOLE__*` environment variables
(`console.py` names each one).
"""

from __future__ import annotations

from typing import Any

# The synthetic identity Console evaluates the public-coder access profile's Kubernetes
# requests as; RBAC bindings grant this group.
PUBLIC_CODER_GROUP = "haku:access-profile:public-coder"


def _exact_tools(policy_id: str, server: str, tools: list[str]) -> dict[str, Any]:
    return {"id": policy_id, "type": "exact_tools", "tools": {server: tools}}


def _any_of(policy_id: str, *policies: str) -> dict[str, Any]:
    return {"id": policy_id, "type": "any_of", "policies": list(policies)}


# Auto-approval is an explicit per-Agent policy graph. Exact-tool atoms grant standing
# authority only to the named tools; newly reflected tools remain manual-approval by default.
# Conditional atoms remain typed code-owned evaluators. `haku_v1` preserves Haku's reviewed
# authority as of 2026-07-31; other Agents receive no auto-approval unless assigned a root
# policy below.
def _auto_approval_policies() -> list[dict[str, Any]]:
    return [
        # Side-effect-free reads on the shared `grants` server (#4918): `kubernetes_can_i`
        # (SAR access inspection) and `get_grant`. `get_grant` is actor-scoped by
        # construction -- it returns only a grant owned by and applicable to the calling
        # Agent (else not-found), and an Operator cannot call it over MCP at all -- so
        # auto-approving it never exposes another principal's grant. `list_grants` is
        # auto-approved separately, only for the explicit own scope (`grants_own_list`).
        # `create_grant` (widening -- it issues new temporary authority) stays manual;
        # `revoke_grants` (own-relinquish, narrowing) is click-free via `grants_own_revoke`.
        _exact_tools("kubernetes_reads", "grants", ["kubernetes_can_i", "get_grant"]),
        # An Agent listing its OWN grants -- `list_grants(principal=self)` -- is click-free
        # (argument-conditional: it confirms a safe scope before approving). The read is
        # actor-scoped regardless (the grant service filters to the caller's own grants), so
        # this only removes the click; omitting `principal` (the reserved broader read) stays
        # manual.
        {"id": "grants_own_list", "type": "grant_self_list", "server": "grants"},
        # `whoami` returns the caller's own resolved console/MCP principal (durable Agent id +
        # live session id + access profile, or Operator id). It takes no arguments and has no
        # side effects -- a pure identity read of what Console already authenticated the
        # caller as, exposing nothing another principal cannot see about itself -- so it is
        # unconditionally click-free. Its own atom keeps the identity/grant-read boundary
        # legible; `grants_self_introspection` bundles it with `grants_own_list`.
        _exact_tools("grants_whoami", "grants", ["whoami"]),
        # The caller's own self-reads on the `grants` server, bundled so each Agent profile
        # references one self-introspection policy instead of repeating the pair.
        _any_of("grants_self_introspection", "grants_whoami", "grants_own_list"),
        # An Agent's `revoke_grants` only ever relinquishes its OWN grants: the tool filters
        # to the caller (`release_applicable_grants` on the trusted request principal), and
        # `owner_agent_id` is operator-only and rejected for an Agent. A narrowing
        # self-service operation, so click-free, while `create_grant` (widening) stays manual.
        _exact_tools("grants_own_revoke", "grants", ["revoke_grants"]),
        _any_of("public_coder_v1", "kubernetes_reads", "grants_self_introspection", "grants_own_revoke"),
        _any_of("haku_v1", "kubernetes_reads", "grants_self_introspection", "grants_own_revoke"),
        {"id": "manual_review", "type": "never"},
    ]


def _mcp_servers() -> dict[str, Any]:
    return {
        # One server fronting every temporary-grant domain (#4918): create/list/get/release/
        # revoke over the shared envelope, each payload tagged with its `domain` (kubernetes |
        # http), plus the side-effect-free SAR check `kubernetes_can_i`. Both ledgers are
        # Console's own Postgres -- credential-free. Exposed to EVERY access profile (operator
        # ruling on #4986): any Agent may ask. `create_grant` is deliberately in NO
        # auto-approval policy: grant creation requires a manually approved source ToolCall
        # (an auto-approved call cannot mint a grant).
        "grants": {"id": "grants", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}
    }


def config() -> dict[str, Any]:
    return {
        "auto_approval_policies": _auto_approval_policies(),
        # Access profiles, not credential bindings, own durable Agent authority. A static bearer
        # and a DCR/OAuth binding assigned to `haku` therefore resolve the same policy/capability
        # bundle. Every profile lists `grants`: even the fallback profile may ASK for a grant --
        # every `grants` call queues for the operator, and create_grant is never
        # auto-approvable (operator ruling on #4986).
        "access_profiles": [
            {"id": "haku", "auto_approval_policy": "haku_v1", "in_process_server_ids": ["grants"]},
            {"id": "public-coder", "auto_approval_policy": "public_coder_v1", "in_process_server_ids": ["grants"]},
            {"id": "manual-review", "auto_approval_policy": "manual_review", "in_process_server_ids": ["grants"]},
        ],
        "default_access_profile_id": "manual-review",
        # Configured Kubernetes access is evaluated as a synthetic, non-login access-profile
        # identity. The username satisfies Console's explicit SAR subject schema; RBAC grants
        # the same deploy-owned group. `system:authenticated` preserves Kubernetes's standard
        # authenticated discovery surface after Console has authenticated the Agent bearer.
        # None of these values is caller-supplied request data. Agent requests execute through
        # the separate haku-kube-api-proxy identity.
        "kubernetes_authorization": {
            "subjects_by_access_profile": {
                "haku": {
                    "username": "haku:access-profile:haku",
                    "groups": ["haku:access-profile:haku", "system:authenticated"],
                },
                "public-coder": {
                    "username": PUBLIC_CODER_GROUP,
                    "groups": [PUBLIC_CODER_GROUP, "system:authenticated"],
                },
            }
        },
        # One or more temporary grants begin only when an approved create_grant call executes.
        # Every item in that call shares one start and expiry. This deploy-owned bound is
        # authoritative even if a client constructs a wider duration than the tool schema
        # recommends.
        "kubernetes_grant_max_lifetime_seconds": 3600,
        # Static machine Agents: each has a fixed durable Agent UUID, globally reserved display
        # name, and bearer bound to one Operator. The bearer and controller-fed Authentik user
        # id are secret leaves, overlaid through `HAKU_CONSOLE__STATIC_AGENTS__<slot>__{TOKEN,
        # OPERATOR_SUBJECT}`. The operator subject is a startup-only Authentik `sub`/user_id
        # seed; the app immediately resolves it to a canonical Operator UUID and never uses it
        # as live request authority.
        "static_agents": {
            "haku": {
                "agent_id": "8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2",
                "display_name": "Haku",
                "access_profile_id": "haku",
            },
            # Everything outside `public_coder_v1` remains approval-wrapped, and Coder cannot
            # approve its own requests.
            "public_coder": {
                "agent_id": "43a833f3-2b1c-4a04-9b8c-1df0cedfb79e",
                "display_name": "public-coder-agent",
                "access_profile_id": "public-coder",
            },
        },
        "mcp_server_enabled": False,
        "mcp": {"servers": _mcp_servers()},
    }
