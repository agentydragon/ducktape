"""Canonical construction of haku-console's same-process MCP servers.

The registry holds *builders* (`InProcessServers`): the gmail/google_calendar servers are
built per execution from the acting Operator's Google access token, hostexec from the acting
Operator's Authentik access token, while routine, conversations and index are credential-free.
Trusted caller context for the profile-scoped servers travels in MCP request metadata. See
`mcp_execution.McpExecutionContext`.
"""

from __future__ import annotations

from dataclasses import dataclass

import haku.console.tools.conversations as conversations_tools
import haku.console.tools.gmail as gmail_tools
import haku.console.tools.google_calendar as google_calendar_tools
import haku.console.tools.hostexec as hostexec_tools
import haku.console.tools.http_grants as http_grants_tools
import haku.console.tools.kubernetes as kubernetes_tools
import haku.console.tools.recall_index as recall_index_tools
import haku.console.tools.routine as routine_tools
import haku.console.tools.sandbox as sandbox_tools
from haku.console.config import HostexecConfig
from haku.console.conversation_read_access import ConversationReadAccessPolicy
from haku.console.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp_config import (
    AccessProfile,
    InProcessCredentialKind,
    InProcessServerRegistration,
    InProcessServers,
    const_in_process_server,
)
from haku.console.recall_index_access import RecallIndexAccessPolicy
from haku.console.tools.hostexec_client import HostexecClient, NodeDaemonBroker
from haku.console.tools.hostexec_token import HostexecJwtBearerExchanger
from haku.sandbox.config import SandboxEnvironmentConfig


@dataclass(frozen=True, slots=True)
class HostexecServerConfig:
    """Everything the hostexec builder needs beyond the per-call operator token: the in-scope hosts
    and the Authentik token endpoint (derived once from the operator OIDC issuer at composition)."""

    config: HostexecConfig
    token_endpoint: str
    broker: NodeDaemonBroker


@dataclass(frozen=True, slots=True)
class SandboxServerConfig:
    """The sandbox lifecycle client plus the environment whose limits its tool schema advertises."""

    client: sandbox_tools.SandboxClient
    environment: SandboxEnvironmentConfig


@dataclass(frozen=True, slots=True)
class InProcessServerDependencies:
    """Runtime collaborators for the in-process servers.

    gmail/google_calendar need none (built per call from the acting Operator's token); routine is
    registered only when its launcher is configured; hostexec only when its config is set; the
    conversations reader only when the Claude runtime is.
    """

    routine_launcher: routine_tools.RoutineLauncher | None = None
    hostexec: HostexecServerConfig | None = None
    # The chat runtime's session store, satisfying `conversations_tools.ConversationReader`
    # structurally — set only when the Claude runtime is configured, since without it there are
    # no sessions to read.
    conversations: conversations_tools.ConversationReader | None = None
    # The semantic index over haku-state's files and past conversations — set only when
    # `config.yaml` lists the server, which is also what requires an embedder to be configured.
    index: recall_index_tools.IndexSearcher | None = None
    kubernetes: kubernetes_tools.KubernetesToolsService | None = None
    http_grants: http_grants_tools.HttpToolsService | None = None
    # The Agent Sandbox lifecycle client and the environment it hands out — set only when
    # `config.yaml` both lists the server and configures `agent_sandbox`.
    sandbox: SandboxServerConfig | None = None
    recall_access_profiles: tuple[AccessProfile, ...] = ()
    configured_recall_index_ids: tuple[str, ...] = ()


def build_in_process_servers(dependencies: InProcessServerDependencies) -> InProcessServers:
    """Build the per-call builder for every configured in-process server."""

    recall_access = RecallIndexAccessPolicy(
        dependencies.recall_access_profiles, configured_index_ids=dependencies.configured_recall_index_ids
    )
    in_process_access = InProcessServerAccessPolicy(dependencies.recall_access_profiles)
    # One profile-DAG read authorizer for conversation history: the `haku_conversations` drilldown
    # and `haku_index`'s chat hits fence rows with the same scope, so ranked retrieval can never
    # surface a conversation the direct read would refuse.
    conversation_reads = ConversationReadAccessPolicy(dependencies.recall_access_profiles)
    servers: InProcessServers = {
        gmail_tools.GMAIL_SERVER_ID: InProcessServerRegistration(
            builder=lambda token: gmail_tools.build_mcp(gmail_tools.build_gmail_client_from_token(token)),
            credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION,
        ),
        google_calendar_tools.GOOGLE_CALENDAR_SERVER_ID: InProcessServerRegistration(
            builder=lambda token: google_calendar_tools.build_mcp(
                google_calendar_tools.build_calendar_client_from_token(token)
            ),
            credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION,
        ),
    }
    if dependencies.routine_launcher is not None:
        servers[routine_tools.HAKU_ROUTINE_SERVER_ID] = const_in_process_server(
            routine_tools.build_mcp(dependencies.routine_launcher)
        )
    if (conversations := dependencies.conversations) is not None:
        servers[conversations_tools.HAKU_CONVERSATIONS_SERVER_ID] = InProcessServerRegistration(
            builder=lambda _token: conversations_tools.build_mcp(
                conversations, access=in_process_access, conversation_reads=conversation_reads
            ),
            credential_kind=InProcessCredentialKind.NONE,
            authorizer=in_process_access.authorizer_for(conversations_tools.HAKU_CONVERSATIONS_SERVER_ID),
        )
    if (index := dependencies.index) is not None:
        servers[recall_index_tools.HAKU_INDEX_SERVER_ID] = InProcessServerRegistration(
            builder=lambda _token: recall_index_tools.build_mcp(
                index, access=recall_access, conversation_reads=conversation_reads
            ),
            credential_kind=InProcessCredentialKind.NONE,
            authorizer=recall_access.authorize_index_tool,
        )
    if (kubernetes := dependencies.kubernetes) is not None:
        servers[kubernetes_tools.KUBERNETES_SERVER_ID] = InProcessServerRegistration(
            builder=lambda _token: kubernetes_tools.build_mcp(kubernetes),
            credential_kind=InProcessCredentialKind.NONE,
            authorizer=in_process_access.authorizer_for(kubernetes_tools.KUBERNETES_SERVER_ID),
        )
    if (http_grants := dependencies.http_grants) is not None:
        servers[http_grants_tools.HTTP_GRANTS_SERVER_ID] = InProcessServerRegistration(
            builder=lambda _token: http_grants_tools.build_mcp(http_grants),
            credential_kind=InProcessCredentialKind.NONE,
            authorizer=in_process_access.authorizer_for(http_grants_tools.HTTP_GRANTS_SERVER_ID),
        )
    if (sandbox := dependencies.sandbox) is not None:
        servers[sandbox_tools.SANDBOX_SERVER_ID] = InProcessServerRegistration(
            builder=lambda _token: sandbox_tools.build_mcp(sandbox.client, sandbox.environment),
            credential_kind=InProcessCredentialKind.NONE,
            authorizer=in_process_access.authorizer_for(sandbox_tools.SANDBOX_SERVER_ID),
        )
    if (hostexec := dependencies.hostexec) is not None:
        daemon_ids = {host: entry.daemon_id for host, entry in hostexec.config.hosts.items()}
        audience_client_ids = {host: entry.audience_client_id for host, entry in hostexec.config.hosts.items()}
        servers[hostexec_tools.HOSTEXEC_SERVER_ID] = InProcessServerRegistration(
            builder=lambda token: hostexec_tools.build_mcp(
                HostexecClient(
                    daemon_ids=daemon_ids,
                    exchange=HostexecJwtBearerExchanger(
                        operator_token=token,
                        token_endpoint=hostexec.token_endpoint,
                        audience_client_ids=audience_client_ids,
                        scope=hostexec.config.exchange_scope,
                    ).exchange,
                    broker=hostexec.broker,
                )
            ),
            credential_kind=InProcessCredentialKind.OPERATOR_LOGIN_IDENTITY,
        )
    return servers
