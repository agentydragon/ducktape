"""The console's non-secret deploy catalog (`haku.console.mcp_config.ConsoleConfigFile`),
rendered into the `haku-console-config` ConfigMap. Secret leaves stay out of it: the
Deployment overlays them from Secrets through `HAKU_CONSOLE__*` environment variables
(`console.py` names each one).
"""

from __future__ import annotations

from typing import Any

from cluster.cdk8s.ssh_mcp.config import MCP_URL

# Every fixed-repository GitHub read policy grants the same tool list; only the trusted
# owner/repository differs. search_pull_requests is safe only with its matching owner/repo
# arguments and no query-level `repo:` qualifier; search_code only with one unquoted
# `repo:<owner>/<repo>` qualifier. The typed policy evaluator verifies both boundaries.
_REPO_READ_TOOLS = [
    "actions_get",
    "actions_list",
    "find_duplicate",
    "get_commit",
    "get_file_blame",
    "get_file_contents",
    "get_job_logs",
    "get_label",
    "get_latest_release",
    "get_release_by_tag",
    "get_tag",
    "issue_dependency_read",
    "issue_read",
    "list_branches",
    "list_commits",
    "list_issue_fields",
    "list_issue_types",
    "list_issues",
    "list_pull_requests",
    "list_releases",
    "list_repository_collaborators",
    "list_tags",
    "pull_request_read",
    "search_issues",
    "search_pull_requests",
    "search_code",
]


def _repository_reads(policy_id: str, owner: str, repository: str) -> dict[str, Any]:
    return {
        "id": policy_id,
        "type": "github_repository",
        "server": "github",
        "owner": owner,
        "repository": repository,
        "tools": list(_REPO_READ_TOOLS),
    }


def _exact_tools(policy_id: str, server: str, tools: list[str]) -> dict[str, Any]:
    return {"id": policy_id, "type": "exact_tools", "tools": {server: tools}}


def _any_of(policy_id: str, *policies: str) -> dict[str, Any]:
    return {"id": policy_id, "type": "any_of", "policies": list(policies)}


def _remote_oauth_server(server_id: str, url: str, client_registration: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": server_id,
        "backend": {
            "kind": "remote_mcp",
            "url": url,
            "auth": {"kind": "remote_server_oauth", "client_registration": client_registration},
        },
    }


def _static_bearer_server(server_id: str, url: str) -> dict[str, Any]:
    return {"id": server_id, "backend": {"kind": "remote_mcp", "url": url, "auth": {"kind": "static_bearer"}}}


# Auto-approval is an explicit per-Agent policy graph. Exact-tool atoms grant standing
# authority only to the named tools; newly reflected tools remain manual-approval by default.
# Conditional atoms remain typed code-owned evaluators. `haku_v1` preserves Haku's reviewed
# authority as of 2026-07-31; other Agents receive no auto-approval unless assigned a root
# policy below.
def _auto_approval_policies() -> list[dict[str, Any]]:
    return [
        _exact_tools(
            "grocy_reads",
            "grocy-sf",
            [
                "entities_get",
                "entities_list",
                "file_get",
                "get_below_minimum_stock",
                "get_current_user",
                "get_db_changed_time",
                "get_expired_stock",
                "get_expiring_stock",
                "get_product_stock",
                "get_system_info",
                "list_volatile_stock",
                "locations_list",
                "product_groups_list",
                "products_list",
                "quantity_units_list",
                "shopping_list_get",
                "shopping_lists_list",
                "stock_entries_list",
                "stock_get",
            ],
        ),
        # GitHub MCP's normal endpoint exposes its default catalog, including writes. This is
        # the explicit 2026-08-14 upstream read-only subset: new upstream tools intentionally
        # stay manual until reviewed here. `ui_get` reads an MCP App UI resource, not GitHub
        # repository state.
        _exact_tools(
            "github_reads",
            "github",
            [
                "get_me",
                "get_team_members",
                "get_teams",
                "ui_get",
                "find_duplicate",
                "get_label",
                "issue_dependency_read",
                "issue_read",
                "list_issue_fields",
                "list_issue_types",
                "list_issues",
                "search_issues",
                "list_pull_requests",
                "pull_request_read",
                "search_pull_requests",
                "get_commit",
                "get_file_blame",
                "get_file_contents",
                "get_latest_release",
                "get_release_by_tag",
                "get_tag",
                "list_branches",
                "list_commits",
                "list_releases",
                "list_repository_collaborators",
                "list_tags",
                "search_code",
                "search_commits",
                "search_repositories",
                "search_users",
            ],
        ),
        # `get_me` returns only the authenticated caller's own GitHub identity and has no
        # repository or mutation surface. Kept separate so every configured Agent can use the
        # identity read without widening repository-scoped GitHub policies.
        _exact_tools("github_identity_reads", "github", ["get_me"]),
        # public-coder-agent may inspect this public source repository, but not use the
        # Operator's GitHub credential to read other public or private repositories.
        _repository_reads("public_ducktape_reads", "agentydragon", "ducktape"),
        # The coder Agent's own fork, used to stage branches before opening PRs into
        # agentydragon/ducktape. It already has write access there (that's how it opens PRs),
        # so a read grant on its own fork's content adds no exposure beyond what it can
        # already write.
        _repository_reads("public_ducktape_fork_reads", "agentydragon-agent", "ducktape"),
        # public-coder-agent may also inspect the private Gaffer repository. A separate
        # repository-scoped atom, so adding one private source does not widen the Ducktape
        # boundary or accidentally grant access to other repositories.
        _repository_reads("public_gaffer_private_reads", "agentydragon", "gaffer-private"),
        # Any repository confirmed genuinely public -- not bounded to a fixed owner/repo
        # allowlist. `github_public_repository` positively confirms the target repository's
        # visibility with a live, unauthenticated GitHub API call before approving
        # (github_policy/repository.py) rather than inferring "public" from the absence of a
        # restriction: the operator's GitHub OAuth token behind this connection can also reach
        # private repos it can see (agentydragon/gaffer-private included), so a bare owner/repo
        # match would let a call read any of those just by naming it. The target repository is
        # derived the same way as for the fixed-repo policies, then checked for public
        # visibility instead of compared against a configured pair.
        {
            "id": "public_github_reads",
            "type": "github_public_repository",
            "server": "github",
            "tools": list(_REPO_READ_TOOLS),
        },
        _any_of(
            "public_coder_github_reads",
            "public_ducktape_reads",
            "public_ducktape_fork_reads",
            "public_gaffer_private_reads",
            "public_github_reads",
        ),
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
        # (argument-conditional, the same shape as `public_*_github_reads` confirming a safe
        # scope before approving). The read is actor-scoped regardless (the grant service
        # filters to the caller's own grants), so this only removes the click; omitting
        # `principal` (the reserved broader read) stays manual.
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
        # A `kubernetes_passthrough` atom (`kubectl_passthrough_redundancy_check`) is
        # deliberately absent: direct-SAR coverage must not auto-deny the operator-linked
        # passthrough route while the public-coder kubeconfig cannot execute its required
        # POST/SPDY transport through haku-kubeapi and only the passthrough service carries
        # the working WebSocket path.
        _any_of(
            "public_coder_v1",
            "public_coder_github_reads",
            "github_identity_reads",
            "kubernetes_reads",
            "grants_self_introspection",
            "grants_own_revoke",
        ),
        _exact_tools(
            "haku_sandbox_control",
            "sandbox",
            ["provision_sandbox", "exec_sandbox", "get_sandbox_info", "list_sandboxes", "dispose_sandbox"],
        ),
        _any_of(
            "haku_v1",
            "grocy_reads",
            "github_reads",
            "github_identity_reads",
            "haku_sandbox_control",
            "kubernetes_reads",
            "grants_self_introspection",
            "grants_own_revoke",
        ),
        {"id": "manual_review", "type": "never"},
    ]


def _mcp_servers() -> dict[str, Any]:
    return {
        # The root endpoint exposes GitHub's normal default toolsets, including writes. Haku
        # receives transparent access only to the explicit github_reads policy; every other
        # tool stays in the Console's per-call operator approval queue; tools in
        # agent_tool_denylist are unavailable to Agents entirely.
        "github": {
            "id": "github",
            # GitHub's hosted MCP catalog is expensive to reflect and changes infrequently:
            # refresh it at most every 15 minutes (other servers keep the shared 60s default).
            "catalog_refresh_interval_seconds": 900,
            # GitHub Copilot delegation mutates repositories and creates external Agent jobs;
            # unavailable to every Haku Agent even when a client retains an older tool schema.
            "agent_tool_denylist": [
                "assign_copilot_to_issue",
                "create_pull_request_with_copilot",
                "get_copilot_job_status",
                "request_copilot_review",
            ],
            "backend": {
                "kind": "remote_mcp",
                "url": "https://api.githubcopilot.com/mcp/",
                # Actions is not in GitHub MCP's default toolsets. Keep the default catalog
                # and add its read/write Actions catalog; policy still decides which calls
                # receive standing authority.
                "headers": {"X-MCP-Toolsets": "default,actions"},
                "auth": {
                    "kind": "remote_server_oauth",
                    # No `scopes` key: `scopes: []` sends an explicit *empty*-scope OAuth
                    # request (mcp/operator_oauth.py's `oauth.scopes is not None` check),
                    # while omitting it asks GitHub's server for its own default, "the full
                    # supported set". The real permission boundary is the OAuth App's
                    # allowed scopes on GitHub's side plus haku-console's own approval policy.
                    # GitHub's hosted MCP does not support Dynamic Client Registration, so
                    # its organization-owned GitHub App supplies this pre-registered
                    # confidential client; client_id/client_secret arrive from the
                    # haku-console-github-mcp-client-credentials Secret.
                    "client_registration": {
                        "kind": "preregistered",
                        "token_endpoint_auth_method": "client_secret_post",
                    },
                },
            },
        },
        "grocy_sf": _remote_oauth_server(
            "grocy-sf", "https://grocy-mcp-sf.allegedly.works/mcp", {"kind": "dynamic", "client_name": "Haku Console"}
        ),
        # `sandbox` (haku/console/tools/sandbox.py): claim a warm Haku sandbox from the
        # `agent_sandbox` pool, bootstrap it, run bounded bash via pods/exec, dispose it.
        # Credential-free -- Console's own ServiceAccount holds the claim/exec RBAC
        # (cluster/k8s/haku/workspaces/app/haku-console-sandbox-role.yaml). By operator
        # directive the whole surface in `haku_sandbox_control` auto-approves so Haku drives
        # its own box tap-free: exec_sandbox is arbitrary bash, but no more than the direct
        # `kubectl exec` Haku's SA can already run in haku-sandbox, and dispose_sandbox only
        # releases the ephemeral claim Haku itself created. `in_process_server_ids` on the
        # profile is what grants access to the server at all.
        "sandbox": {"id": "sandbox", "backend": {"kind": "in_process", "credential": {"kind": "none"}}},
        # One server fronting every temporary-grant domain (#4918): create/list/get/release/
        # revoke over the shared envelope, each payload tagged with its `domain` (kubernetes |
        # http), plus the side-effect-free SAR check `kubernetes_can_i`. Both ledgers are
        # Console's own Postgres -- credential-free. Exposed to EVERY access profile (operator
        # ruling on #4986): any Agent may ask. `create_grant` is deliberately in NO
        # auto-approval policy: grant creation requires a manually approved source ToolCall
        # (an auto-approved call cannot mint a grant).
        "grants": {"id": "grants", "backend": {"kind": "in_process", "credential": {"kind": "none"}}},
        "ssh": _static_bearer_server("ssh", MCP_URL),
        # kubectl-passthrough-mcp forwards the approving operator's own OAuth token straight to
        # kube-apiserver (cluster_auth_mode=passthrough) -- no group override, no scoped
        # credential of its own. RBAC is agentydragon's real cluster-admin binding
        # (oidc-ksbx-agentydragon-admin, cluster/k8s/agents/kubectl-passthrough-mcp/app/), so
        # every kubectl-shaped tool runs with full cluster-admin once approved; the approval
        # click in trusted console chrome is the only gate, by design (haku/docs/security.md).
        "kubectl_passthrough_mcp": _remote_oauth_server(
            "kubectl-passthrough-mcp",
            "https://kubectl-passthrough-mcp.allegedly.works/mcp",
            # Authentik has no open Dynamic Client Registration endpoint (kubernetes-mcp-server
            # mirrors Authentik's own OAuth metadata, which omits registration_endpoint), so
            # DCR would 401 against the guessed {server}/register fallback. The pre-registered
            # public/PKCE client_id from the kubectl_passthrough_mcp Authentik provider
            # (tf/gitops/agent-machine-access/main.tf) already allows multiple redirect URIs,
            # haku-console's operator-auth callback included; PKCE plus per-request
            # redirect_uri validation secure each caller independently.
            {"kind": "preregistered", "client_id": "kubectl-passthrough-mcp"},
        ),
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
            {"id": "haku", "auto_approval_policy": "haku_v1", "in_process_server_ids": ["grants", "sandbox"]},
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
                    "username": "haku:access-profile:public-coder",
                    "groups": ["haku:access-profile:public-coder", "system:authenticated"],
                },
            }
        },
        # One or more temporary grants begin only when an approved create_grant call executes.
        # Every item in that call shares one start and expiry. This deploy-owned bound is
        # authoritative even if a client constructs a wider duration than the tool schema
        # recommends.
        "kubernetes_grant_max_lifetime_seconds": 3600,
        # The one Agent Sandbox environment the `sandbox` server hands out: the Haku pool in
        # cluster/k8s/haku/workspaces/ and the reviewed bootstrap each claim runs. Each claim
        # records the pod-describing fields it was created for, so editing one leaves live
        # claims usable and flags them in `warnings`; the budgets are read live and never
        # recorded (haku/sandbox/README.md).
        "agent_sandbox": {
            "sandbox": {
                "namespace": "haku-sandbox",
                "warm_pool": "haku",
                "container": "workspace",
                "default_cwd": "/workspace/haku-state",
                # initial_ttl must exceed provisioning_timeout + bootstrap.timeout; exec
                # extension must be >= max_exec_timeout (validated at startup).
                "initial_ttl_seconds": 28800,
                "exec_ttl_extension_seconds": 7200,
                "provisioning_timeout_seconds": 600,
                "max_exec_timeout_seconds": 300,
                "max_output_bytes": 100000,
            },
            "bootstrap": {
                "cwd": "/workspace",
                "timeout_seconds": 300,
                # The reviewed per-claim bootstrap is one baked script -- egress CA trust, git
                # identity, git credentials, and the haku-state checkout -- kept in its native
                # file (cluster/k8s/haku/workspaces/image/haku-sandbox-setup.sh) so shfmt/
                # shellcheck lint it. Changing bootstrap behavior therefore means an image
                # rebuild + rollout, not a ConfigMap edit.
                "script": "set -euo pipefail\n/usr/local/bin/haku-sandbox-setup.sh\n",
            },
        },
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
            # Coder may read the explicitly named Ducktape and Gaffer repositories without
            # interrupting the Operator. Everything else remains approval-wrapped, and Coder
            # cannot approve its own requests.
            "public_coder": {
                "agent_id": "43a833f3-2b1c-4a04-9b8c-1df0cedfb79e",
                "display_name": "public-coder-agent",
                "access_profile_id": "public-coder",
            },
        },
        "mcp": {"servers": _mcp_servers()},
    }
