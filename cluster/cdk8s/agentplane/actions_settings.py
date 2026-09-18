"""Generates agentplane-{staging,testing}/actions/settings.yaml's content -- the Action
Service's own `Settings.config_file` input (x/agentplane/actions), mounted read-only by
the Deployment. Not related to app_settings.py, which is the integration app's own
`Settings` shape.
"""

from __future__ import annotations

from cluster.cdk8s.agentplane.actions_testing_fixtures import MCP_EVERYTHING_NAME, MCP_EVERYTHING_PORT, NAMESPACE

# The push services staging's web-push subscriptions are allowed to target -- also fed
# into generate_manifests.py's CiliumNetworkPolicy egress rule for the actions Deployment,
# so the policy can't drift from what the app actually accepts.
WEB_PUSH_ALLOWED_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com")


def staging_settings() -> dict:
    return {
        "allowed_service_account_namespaces": ["agentplane-staging"],
        "web_push": {
            "subject": "mailto:agentydragon@gmail.com",
            "public_base_url": "https://agentplane-staging.allegedly.works",
            "allowed_push_hosts": list(WEB_PUSH_ALLOWED_HOSTS),
        },
        "mcp_servers": {
            "github": {
                "server_id": "github",
                "provider": "github",
                "server_url": "https://api.githubcopilot.com/mcp/",
                "client_id": "configured-by-secret",
                "client_secret_file": "/etc/agentplane-github/client_secret",
                "redirect_uri": "https://agentplane-staging.allegedly.works/mcp-linkage/callback",
            },
            "kubernetes": {
                "server_id": "kubernetes",
                "provider": "kubernetes",
                "server_url": "https://kubectl-passthrough-mcp.allegedly.works/mcp",
                "client_id": "kubectl-passthrough-mcp",
                "redirect_uri": "https://agentplane-staging.allegedly.works/mcp-linkage/callback",
            },
        },
        "action_groups": {
            "github": {
                "title": "GitHub MCP",
                "description": "GitHub's operator-linked MCP tools; every Action remains subject to operator approval.",
                "executor": {
                    "kind": "mcp",
                    "description": "GitHub MCP executed with the linked operator GitHub account.",
                    "config": {
                        "transport": "streamable-http",
                        "url": "https://api.githubcopilot.com/mcp/",
                        "server_id": "github",
                        "auth": "oauth",
                    },
                },
            },
            "kubernetes": {
                "title": "Kubernetes MCP",
                "description": "Kubernetes passthrough MCP tools; every Action remains subject to operator approval.",
                "executor": {
                    "kind": "mcp",
                    "description": "Kubernetes MCP executed with the linked operator Kubernetes identity.",
                    "config": {
                        "transport": "streamable-http",
                        "url": "https://kubectl-passthrough-mcp.allegedly.works/mcp",
                        "server_id": "kubernetes",
                        "auth": "oauth",
                    },
                },
            },
            "ssh": {
                "title": "SSH",
                "description": "SSH commands on configured targets; every Action remains subject to operator approval.",
                "executor": {
                    "kind": "mcp",
                    "description": "Standalone SSH MCP backend; Agentplane retains approval and execution authority.",
                    "config": {
                        "transport": "streamable-http",
                        "url": "http://ssh-mcp.ssh-mcp.svc.cluster.local:8080/mcp",
                        "auth": "static_bearer",
                        "bearer_file": "/run/secrets/ssh-mcp/bearer-token",
                    },
                },
            },
        },
    }


def testing_settings() -> dict:
    return {
        "allowed_service_account_namespaces": ["agentplane-testing"],
        "mcp_servers": {
            "example": {
                "server_id": "example",
                "provider": "example",
                "server_url": "http://agentplane-oauth-fixture.agentplane-testing.svc.cluster.local:8080/mcp",
                "client_id": "agentplane-testing-mcp",
                "client_secret_file": "/etc/agentplane-mcp/client-secret",
                "redirect_uri": "https://agentplane-testing.allegedly.works/mcp-linkage/callback",
                "scopes": ["openid"],
            }
        },
        "action_groups": {
            "everything": {
                "title": "Upstream Everything",
                "description": "Credentialless MCP reference server for testing acceptance.",
                "executor": {
                    "kind": "mcp",
                    "description": "Community-built Everything image; no user account, workload token, or mounted credentials.",
                    "config": {
                        "transport": "streamable-http",
                        "url": f"http://{MCP_EVERYTHING_NAME}.{NAMESPACE}.svc.cluster.local:{MCP_EVERYTHING_PORT}/mcp",
                        "auth": "none",
                    },
                },
            },
            "example": {
                "title": "OAuth Example",
                "description": "Dex-backed OAuth-linked MCP fixture for testing acceptance of the linkage flow.",
                "executor": {
                    "kind": "mcp",
                    "description": "MCP tool protected by Dex-issued JWTs; no real credentials.",
                    "config": {
                        "transport": "streamable-http",
                        "url": "http://agentplane-oauth-fixture.agentplane-testing.svc.cluster.local:8080/mcp",
                        "server_id": "example",
                        "auth": "oauth",
                    },
                },
            },
        },
    }
