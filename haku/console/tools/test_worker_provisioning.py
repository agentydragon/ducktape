"""End-to-end contracts for the worker sandbox provisioning read.

The tool runs over a real migrated Postgres session store and the same `SessionService` read the
SPA uses. Only the Kubernetes claim client is recorded, so the test can supply a provisioning view
without replacing the session runtime or its operator/profile authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastmcp import Client, FastMCP

from haku.console.config import HarnessRegistrationConfig
from haku.console.conftest import console_sessions, operator_identity_store
from haku.console.conversation_read_access import ConversationReadAccessPolicy
from haku.console.grants.principal import RequestPrincipal
from haku.console.harnesses.kind import HarnessKind
from haku.console.identity.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.mcp.execution import (
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
    mcp_execution_request_meta,
)
from haku.console.mcp_config import AccessProfile
from haku.console.notifications.session_wakes import SessionWakes
from haku.console.session.conversation_views import SessionProvisioningView
from haku.console.session.runtime import SessionService
from haku.console.session.sandbox_claims import SandboxProvisioningView
from haku.console.session.status import SessionStatus
from haku.console.session.store import Store
from haku.console.session.system_prompt import SystemPromptTemplate
from haku.console.tools import workers as workers_tools
from haku.console.tools.workers import DispatchedWorker
from haku.console.x.runtime_catalog import execution_registry, harness_registration
from haku.console.x.testing.recording_claims import RecordingClaims, fixed_provisioning_view

_ORCHESTRATOR_PROFILE = "orchestrator"
_WORKER_PROFILE = "worker"
_OUTSIDER_PROFILE = "outsider"
_PROFILES = (
    AccessProfile(
        id=_ORCHESTRATOR_PROFILE,
        auto_approval_policy="safe_reads",
        in_process_server_ids={workers_tools.WORKERS_SERVER_ID},
        can_read_profiles={_WORKER_PROFILE},
    ),
    AccessProfile(id=_WORKER_PROFILE, auto_approval_policy="safe_reads"),
    AccessProfile(
        id=_OUTSIDER_PROFILE, auto_approval_policy="safe_reads", in_process_server_ids={workers_tools.WORKERS_SERVER_ID}
    ),
)
_READS = ConversationReadAccessPolicy(_PROFILES)
_WORKER_AGENT_ID = UUID("40000000-0000-4000-8000-00000000ee01")
_ORCHESTRATOR_AGENT_ID = UUID("40000000-0000-4000-8000-00000000ee02")


def _worker_harness_config() -> HarnessRegistrationConfig:
    return HarnessRegistrationConfig(
        agent_id=str(_WORKER_AGENT_ID),
        namespace="test-harness-sandbox",
        warm_pool="test-codex-pool",
        claim_prefix="test-codex",
        harness_label="test-codex",
        cwd="/test/workspace",
        session_ttl_seconds=7200,
        https_proxy="http://test-egress-proxy.invalid:8080",
        ca_bundle="/test/ca/bundle.pem",
        no_proxy="test-service.invalid,test-cluster.invalid",
        mcp_url="http://test-console.invalid:9090/mcp",
        implementation={
            "kind": "codex_app_server",
            "model": "test-codex-model",
            "provider_id": "test-provider",
            "provider_name": "Test OpenAI-compatible provider",
            "api_base_url": "http://test-litellm.invalid/v1",
            "api_key_env_var": "OPENAI_API_KEY",
            "github_token_placeholder": "test-github-token-placeholder",
        },
    )


@dataclass(frozen=True, slots=True)
class _Env:
    service: SessionService
    claims: RecordingClaims
    mcp: FastMCP
    operator_id: UUID


@dataclass(frozen=True, slots=True)
class _ProvisionedSession:
    session_id: UUID
    view: SandboxProvisioningView


@pytest.fixture
async def env(migrated_db_url: str) -> _Env:
    sessions = console_sessions(migrated_db_url)
    identity_store = operator_identity_store(migrated_db_url)
    operator_id = await identity_store.resolve_configured_external_user_key("worker-provisioning-op")
    authority = PostgresAgentAuthority(
        sessions,
        public_base_url="https://haku.test",
        operator_identity_store=identity_store,
        access_profiles=tuple(profile.id for profile in _PROFILES),
        default_access_profile_id=_WORKER_PROFILE,
    )
    await authority.reconcile_static_agents(
        [
            StaticAgentDefinition(
                agent_id=_WORKER_AGENT_ID,
                display_name="Provisioning Test Worker",
                operator_id=operator_id,
                secret_reference="env:HAKU_CONSOLE_TEST_PROVISIONING_WORKER_TOKEN",
                token_fingerprint=fingerprint_static_token("worker-provisioning-token"),
                access_profile_id=_WORKER_PROFILE,
            )
        ]
    )
    claims = RecordingClaims()
    service = SessionService(
        execution_registry(
            harness_registration(
                _worker_harness_config(),
                claims,
                system_prompt=SystemPromptTemplate(""),
                access_profile_id=_WORKER_PROFILE,
            )
        ),
        Store(sessions),
        SessionWakes(migrated_db_url),
    )
    return _Env(
        service=service,
        claims=claims,
        mcp=workers_tools.build_mcp(service, conversation_reads=_READS),
        operator_id=operator_id,
    )


async def _dispatch(env: _Env) -> UUID:
    context = McpExecutionContext(
        caller=OperatorMcpExecutionCaller(operator_id=env.operator_id),
        tool_call_id=None,
        approving_operator_id=None,
        approval_policy_id=None,
    )
    async with Client(env.mcp) as client:
        result = await client.call_tool(
            "dispatch_worker",
            {
                "agent_id": str(_WORKER_AGENT_ID),
                "harness_kind": HarnessKind.CODEX_APP_SERVER,
                "prompt": "Report your provisioning state.",
            },
            meta=mcp_execution_request_meta(context),
        )
    return DispatchedWorker.model_validate(result.structured_content).session_id


@pytest.fixture
async def provisioned_session(env: _Env) -> _ProvisionedSession:
    session_id = await _dispatch(env)
    await env.service.allocate(env.operator_id, session_id)
    view = fixed_provisioning_view(session_id)
    env.claims.answer(view)
    return _ProvisionedSession(session_id=session_id, view=view)


def _meta(env: _Env, profile: str) -> dict[str, object]:
    return mcp_execution_request_meta(
        McpExecutionContext(
            caller=AgentMcpExecutionCaller(
                principal=RequestPrincipal(agent_id=_ORCHESTRATOR_AGENT_ID, session_id=None, access_profile_id=profile),
                operator_id=env.operator_id,
            ),
            tool_call_id=None,
            approving_operator_id=None,
            approval_policy_id=None,
        )
    )


async def _call(env: _Env, session_id: UUID, *, profile: str = _ORCHESTRATOR_PROFILE, raise_on_error: bool = False):
    async with Client(env.mcp) as client:
        return await client.call_tool(
            "get_worker_provisioning",
            {"session_id": str(session_id)},
            meta=_meta(env, profile),
            raise_on_error=raise_on_error,
        )


async def test_get_worker_provisioning_returns_the_existing_view_without_approval(
    env: _Env, provisioned_session: _ProvisionedSession
) -> None:
    result = await _call(env, provisioned_session.session_id)

    assert not result.is_error
    view = SessionProvisioningView.model_validate(result.structured_content)
    assert view.session_id == provisioned_session.session_id
    assert view.status is SessionStatus.PROVISIONING
    assert view.sandbox == provisioned_session.view
    assert env.claims.inspected == [provisioned_session.session_id]


async def test_get_worker_provisioning_refuses_a_session_outside_the_read_scope(
    env: _Env, provisioned_session: _ProvisionedSession
) -> None:
    result = await _call(env, provisioned_session.session_id, profile=_OUTSIDER_PROFILE)

    assert result.is_error
    assert "conversation access denied" in str(result.content)
    assert env.claims.inspected == []


async def test_get_worker_provisioning_reports_an_unknown_session_as_not_found(env: _Env) -> None:
    result = await _call(env, uuid4())

    assert result.is_error
    assert "worker session not found" in str(result.content)
    assert env.claims.inspected == []


if __name__ == "__main__":
    pytest_bazel.main()
