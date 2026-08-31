"""End-to-end contracts for the `workers` MCP server over the real approval + session runtime.

The tool is exercised the way an orchestrator Agent reaches it: submitted at the real
`ToolCallApplicationService` boundary against a migrated Postgres, gated by the same manual-approval
path `create_grant` takes, then approved through the operator decision endpoint so the real
in-process tool executes and drives the console's own `SessionService`. What is stood in for is only
the Kubernetes sandbox (an empty runtime registry — `dispatch_worker` opens an idle conversation and
seeds its prompt, both pure database writes; sandbox provisioning is the async allocator's job and
is not this tool's work), never the platform: the store, the launch authorizer, the durable Agent
authority, and the approval ledger are all real.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from more_itertools import one
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conftest import TEST_OPERATOR_IDENTITY, TEST_OPERATOR_OIDC, console_sessions, write_config
from haku.console.conversation.prompt_origin import SPA_ORIGIN
from haku.console.conversation_read_access import ConversationReadAccessPolicy
from haku.console.database_schema import (
    Agent,
    Conversation,
    CredentialBinding,
    Session,
    StaticCredential,
    SubmittedPrompt,
)
from haku.console.harnesses.kind import HarnessKind
from haku.console.identity.authorization import PostgresAgentAuthority, fingerprint_static_token
from haku.console.identity.operator_identity import OperatorIdentityTrust
from haku.console.identity.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.mcp.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp.in_process_servers import InProcessServerDependencies, build_in_process_servers
from haku.console.mcp.tool_call_service import ToolCallApplicationService
from haku.console.mcp_config import (
    ConsoleConfigFile,
    InProcessCredentialKind,
    InProcessServerRegistration,
    InProcessServers,
)
from haku.console.notifications.session_wakes import SessionWakes
from haku.console.session.launch_identity import HarnessLaunchAuthorizer
from haku.console.session.runtime import SessionService
from haku.console.session.store import Store
from haku.console.tool_call_actor import AgentActor
from haku.console.tool_calls import SubmitToolCallRequest, ToolCallStatus
from haku.console.tools import workers as workers_tools
from haku.console.x.runtime import HarnessKey
from haku.console.x.runtime_catalog import execution_registry

# One operator owns the orchestrator, the worker, and approves the dispatch — the v0 single-operator
# perimeter, and what makes the approving operator the worker's owner (§ `_dispatching_operator`).
_OPERATOR_SUBJECT = "operator-sub"
_ORCHESTRATOR_AGENT_ID = UUID("40000000-0000-4000-8000-00000000dd01")
_WORKER_AGENT_ID = UUID("40000000-0000-4000-8000-00000000dd02")
_ORCHESTRATOR_TOKEN = "workers-orchestrator-token"
_WORKER_TOKEN = "workers-worker-token"
_ORCHESTRATOR_PROFILE = "orchestrator"
_WORKER_PROFILE = "worker"
_PROMPT = "Refactor the widget module and open a PR."


def _config() -> dict[str, Any]:
    return {
        "static_agents": {
            "orchestrator": {
                "agent_id": str(_ORCHESTRATOR_AGENT_ID),
                "display_name": "Orchestrator",
                "token": _ORCHESTRATOR_TOKEN,
                "operator_subject": _OPERATOR_SUBJECT,
                "access_profile_id": _ORCHESTRATOR_PROFILE,
            },
            "worker": {
                "agent_id": str(_WORKER_AGENT_ID),
                "display_name": "Public Coder",
                "token": _WORKER_TOKEN,
                "operator_subject": _OPERATOR_SUBJECT,
                "access_profile_id": _WORKER_PROFILE,
            },
        },
        "auto_approval_policies": [{"id": "manual", "type": "never"}],
        "access_profiles": [
            {"id": _ORCHESTRATOR_PROFILE, "auto_approval_policy": "manual", "in_process_server_ids": ["workers"]},
            {"id": _WORKER_PROFILE, "auto_approval_policy": "manual"},
        ],
        "default_access_profile_id": _WORKER_PROFILE,
        "mcp": {
            "servers": {"workers": {"id": "workers", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}}
        },
    }


def _session_runtime(
    sessions: async_sessionmaker[AsyncSession], identity_store: PostgresOperatorIdentityStore, db_url: str
) -> SessionService:
    """A real SessionService whose launch authorizer only launches the worker Agent on codex.

    The runtime registry is empty: `dispatch_worker` never provisions a sandbox (that is the
    allocator's later, asynchronous work), so nothing here consults it — an honest registry for the
    database-only open-and-seed the tool actually performs.
    """
    authority = PostgresAgentAuthority(
        sessions,
        public_base_url="https://haku.test",
        operator_identity_store=identity_store,
        access_profiles=(_ORCHESTRATOR_PROFILE, _WORKER_PROFILE),
        default_access_profile_id=_WORKER_PROFILE,
    )
    launch_authorizer = HarnessLaunchAuthorizer(
        authority,
        launchable_agent_ids={_WORKER_AGENT_ID},
        registered_harness_identities={HarnessKey(_WORKER_AGENT_ID, HarnessKind.CODEX_APP_SERVER)},
        profile_harness_kinds={_WORKER_PROFILE: {HarnessKind.CODEX_APP_SERVER}},
    )
    return SessionService(
        execution_registry(), Store(sessions), SessionWakes(db_url), launch_authorizer=launch_authorizer
    )


async def _resolve_agent_actor(sessions: async_sessionmaker[AsyncSession], token: str) -> AgentActor:
    async with sessions() as session:
        binding_id, agent_id, operator_id, access_profile_id = (
            await session.execute(
                select(
                    CredentialBinding.binding_id,
                    CredentialBinding.agent_id,
                    Agent.owner_operator_id,
                    Agent.access_profile_id,
                )
                .join(StaticCredential, StaticCredential.binding_id == CredentialBinding.binding_id)
                .join(Agent, Agent.agent_id == CredentialBinding.agent_id)
                .where(StaticCredential.credential_fingerprint == fingerprint_static_token(token))
            )
        ).one()
    return AgentActor(
        agent_id=agent_id, operator_id=operator_id, binding_id=binding_id, access_profile_id=access_profile_id
    )


async def _conversations(sessions: async_sessionmaker[AsyncSession]) -> list[Conversation]:
    async with sessions() as session:
        return list((await session.scalars(select(Conversation))).all())


async def _session_of(sessions: async_sessionmaker[AsyncSession], conversation_id: UUID) -> Session:
    async with sessions() as session:
        return (await session.scalars(select(Session).where(Session.conversation_id == conversation_id))).one()


async def _submitted_prompts(
    sessions: async_sessionmaker[AsyncSession], conversation_id: UUID
) -> list[SubmittedPrompt]:
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(SubmittedPrompt)
                    .where(SubmittedPrompt.conversation_id == conversation_id)
                    .order_by(SubmittedPrompt.submitted_at)
                )
            ).all()
        )


@dataclass(frozen=True, slots=True)
class _Console:
    """One console app whose `workers` server runs over a real SessionService on the app database."""

    client: TestClient
    sessions: async_sessionmaker[AsyncSession]

    def call[T](self, func: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run one async step on the app's own event loop, where its engine lives."""
        assert self.client.portal is not None
        return self.client.portal.call(func, *args)

    @property
    def _service(self) -> ToolCallApplicationService:
        return cast(ToolCallApplicationService, cast(FastAPI, self.client.app).state.tool_call_service)

    def orchestrator(self) -> AgentActor:
        return self.call(partial(_resolve_agent_actor, self.sessions, _ORCHESTRATOR_TOKEN))

    def dispatch(self, actor: AgentActor, *, agent_id: UUID, harness_kind: HarnessKind, prompt: str) -> dict[str, Any]:
        request = SubmitToolCallRequest(
            server_id=workers_tools.WORKERS_SERVER_ID,
            tool_name="dispatch_worker",
            rationale="fan a subtask onto a worker",
            arguments={"agent_id": str(agent_id), "harness_kind": harness_kind, "prompt": prompt},
            wait_for_ms=0,
        )

        async def submit() -> Any:
            return await self._service.submit_and_wait(req=request, actor=actor)

        return cast(dict[str, Any], self.call(submit).model_dump(mode="json"))

    def approve(self, tool_call_id: str) -> None:
        response = self.client.post(f"/api/tool-calls/{tool_call_id}/decision", json={"decision": "approve"})
        assert response.status_code == 200, response.text
        # decide() dispatches execution as a background task; drain it on the app loop so a sync
        # TestClient can observe the terminal row and the conversation it created.
        self.call(self._service.join_executions)

    def tool_call(self, tool_call_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.client.get(f"/api/tool-calls/{tool_call_id}").json())

    def conversations(self) -> Sequence[Conversation]:
        return self.call(partial(_conversations, self.sessions))


@pytest.fixture
def workers_console(
    make_operator_client: Callable[..., Any], migrated_db_url: str, tmp_path: Path
) -> Iterator[_Console]:
    config = _config()
    config_path = write_config(tmp_path / "workers_console.yaml", config)
    profiles = ConsoleConfigFile.model_validate(config).access_profiles
    access = InProcessServerAccessPolicy(tuple(profiles))
    conversation_reads = ConversationReadAccessPolicy(tuple(profiles))
    sessions = console_sessions(migrated_db_url)
    identity_store = PostgresOperatorIdentityStore(
        sessions,
        OperatorIdentityTrust(
            trust_domain=TEST_OPERATOR_IDENTITY.trust_domain, trusted_issuers=frozenset({TEST_OPERATOR_OIDC.issuer})
        ),
    )
    runtime = _session_runtime(sessions, identity_store, migrated_db_url)
    in_process_servers: InProcessServers = {
        workers_tools.WORKERS_SERVER_ID: InProcessServerRegistration(
            builder=lambda _token: workers_tools.build_mcp(runtime, conversation_reads=conversation_reads),
            credential_kind=InProcessCredentialKind.NONE,
            authorizer=access.authorizer_for(workers_tools.WORKERS_SERVER_ID),
        )
    }
    with make_operator_client(config_file=config_path, in_process_servers=in_process_servers) as client:
        yield _Console(client=client, sessions=sessions)


def test_dispatch_worker_returns_a_pending_stub_before_approval(workers_console: _Console) -> None:
    record = workers_console.dispatch(
        workers_console.orchestrator(),
        agent_id=_WORKER_AGENT_ID,
        harness_kind=HarnessKind.CODEX_APP_SERVER,
        prompt=_PROMPT,
    )

    assert record["status"] == ToolCallStatus.PENDING_APPROVAL
    assert record["tool_call_id"] is not None
    # Nothing is launched until the operator approves: no conversation exists yet.
    assert workers_console.conversations() == []


def test_approved_dispatch_opens_the_worker_session_and_seeds_the_prompt(workers_console: _Console) -> None:
    record = workers_console.dispatch(
        workers_console.orchestrator(),
        agent_id=_WORKER_AGENT_ID,
        harness_kind=HarnessKind.CODEX_APP_SERVER,
        prompt=_PROMPT,
    )

    workers_console.approve(record["tool_call_id"])

    finished = workers_console.tool_call(record["tool_call_id"])
    assert finished["status"] == ToolCallStatus.OK

    conversation = one(workers_console.conversations())
    assert conversation.agent_id == _WORKER_AGENT_ID
    assert conversation.harness_kind == HarnessKind.CODEX_APP_SERVER

    # The tool returns {session_id}: the created session's id round-trips through the MCP result.
    session = workers_console.call(partial(_session_of, workers_console.sessions, conversation.conversation_id))
    assert str(session.session_id) in json.dumps(finished["result"])

    prompt = one(
        workers_console.call(partial(_submitted_prompts, workers_console.sessions, conversation.conversation_id))
    )
    assert prompt.text == _PROMPT
    assert prompt.origin == SPA_ORIGIN


def test_build_in_process_servers_registers_workers_only_with_a_session_runtime(migrated_db_url: str) -> None:
    sessions = console_sessions(migrated_db_url)
    runtime = SessionService(execution_registry(), Store(sessions), SessionWakes(migrated_db_url))

    assert workers_tools.WORKERS_SERVER_ID in build_in_process_servers(InProcessServerDependencies(sessions=runtime))
    assert workers_tools.WORKERS_SERVER_ID not in build_in_process_servers(InProcessServerDependencies())


if __name__ == "__main__":
    pytest_bazel.main()
