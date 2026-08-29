"""Focused contracts for the Agent Sandbox Claude chat runtime.

**No channel is imported here, deliberately.** An attachment only selects whether the conversation
gets the shared direct-chat system prompt; setup, answers, silence and live state are durable facts
that channel subscribers project. What a homeserver's messages become is
<channels/matrix/test_conversation.py>, beside the `Turns` that makes them turns.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastapi import HTTPException
from more_itertools import one
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.config import ChatRuntimesConfig, ClaudeCodeImplementationConfig, RuntimeRegistrationConfig
from haku.console.conftest import console_sessions
from haku.console.conversation.history import ConversationHistory
from haku.console.conversation.prompt_origin import SPA_ORIGIN
from haku.console.database_schema import Agent, Conversation, Session
from haku.console.harnesses.kind import HarnessKind
from haku.console.identity.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.mcp_config import ConsoleConfigFile
from haku.console.notifications.session_wakes import SessionWakes
from haku.console.session.conftest import (
    TEST_ACCESS_PROFILE_ID,
    TEST_AGENT_ID,
    age_lease,
    attach_channel,
    configured_runtimes,
    runtime_config,
)
from haku.console.session.launch_identity import ChatLaunchAuthorizer, LaunchAgentRejectedError, LaunchIdentity
from haku.console.session.runtime import (
    ConversationCreateRequest,
    SessionService,
    _transient_database_error,
    create_conversation,
)
from haku.console.session.sandbox_claims import ProvisioningStep, provisioning_view
from haku.console.session.status import OPEN_SESSION_STATUSES, SessionStatus
from haku.console.session.store import ADOPTION_GRACE, BridgeAuthentication, Store
from haku.console.session.system_prompt import SystemPromptTemplate
from haku.console.x.codex_app_server.config import CodexAppServerImplementationConfig
from haku.console.x.runtime import HarnessKey, RuntimeLaunch
from haku.console.x.runtime_catalog import runtime_registration
from haku.console.x.testing.recording_claims import RecordingClaims
from haku.runner.codex.options import CODEX_MODEL_ENV, CODEX_REASONING_EFFORT_ENV


def test_runtime_deployment_wiring_has_no_application_defaults() -> None:
    assert all(field.is_required() for field in RuntimeRegistrationConfig.model_fields.values())
    assert ChatRuntimesConfig.model_fields["claude_code"].is_required()
    assert not ChatRuntimesConfig.model_fields["codex_app_server"].is_required()
    assert not ConsoleConfigFile.model_fields["harnesses"].is_required()


def test_new_conversation_request_rejects_client_supplied_access_profile() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ConversationCreateRequest.model_validate(
            {"agent_id": str(uuid4()), "runtime": HarnessKind.CLAUDE_CODE, "access_profile_id": "admin"}
        )


@pytest.mark.parametrize("body", [{"agent_id": str(uuid4())}, {"runtime": HarnessKind.CLAUDE_CODE}])
def test_new_conversation_request_requires_the_complete_launch_pair(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Field required"):
        ConversationCreateRequest.model_validate(body)


async def test_direct_session_service_has_no_default_agent_or_harness(chat_service, operator_id) -> None:
    with pytest.raises(RuntimeError, match="selected Agent"):
        await chat_service.create(operator_id, harness_kind=HarnessKind.CLAUDE_CODE)
    with pytest.raises(RuntimeError, match="selected harness"):
        await chat_service.create(operator_id, agent_id=TEST_AGENT_ID)


async def test_post_conversation_launch_rejection_is_generic_403() -> None:
    class RejectingService:
        async def create_conversation(self, *_args: Any, **_kwargs: Any) -> None:
            raise LaunchAgentRejectedError("durable internal reason")

    actor = type("Actor", (), {"operator_id": uuid4()})()
    with pytest.raises(HTTPException) as error:
        await create_conversation(
            ConversationCreateRequest(agent_id=uuid4(), runtime=HarnessKind.CLAUDE_CODE),
            actor,
            cast(SessionService, RejectingService()),
        )

    assert error.value.status_code == 403
    assert error.value.detail == "chat launch is not authorized"
    assert "durable internal reason" not in str(error.value)


class _RecordingLaunchAuthorizer:
    def __init__(self, delegate: ChatLaunchAuthorizer) -> None:
        self._delegate = delegate
        self.calls: list[tuple[UUID, str | None, AsyncSession, bool]] = []

    async def __call__(
        self,
        db: AsyncSession,
        operator_id: UUID,
        agent_id: UUID,
        harness_kind: HarnessKind,
        *,
        expected_profile_id: str | None = None,
    ) -> LaunchIdentity:
        assert db.in_transaction()
        self.calls.append((agent_id, expected_profile_id, db, db.in_transaction()))
        return await self._delegate(db, operator_id, agent_id, harness_kind, expected_profile_id=expected_profile_id)


async def test_replacement_pins_identity_after_agent_profile_change_and_shares_store_transaction(
    migrated_db_url: str,
    migrated_sessions,
    migrated_identity_store,
    operator_id: UUID,
    recording_claims: RecordingClaims,
    session_wakes: SessionWakes,
) -> None:
    agent_id = uuid4()
    authority = PostgresAgentAuthority(
        console_sessions(migrated_db_url),
        public_base_url="https://haku.test",
        operator_identity_store=migrated_identity_store,
        access_profiles=("pinned", "current"),
        default_access_profile_id="pinned",
    )
    await authority.reconcile_static_agents(
        [
            StaticAgentDefinition(
                agent_id=agent_id,
                display_name="Pinned Runtime Agent",
                operator_id=operator_id,
                secret_reference="env:PINNED_RUNTIME_AGENT",
                token_fingerprint=fingerprint_static_token("pinned-runtime-token"),
                access_profile_id="pinned",
            )
        ]
    )
    authorizer = _RecordingLaunchAuthorizer(
        ChatLaunchAuthorizer(
            authority,
            launchable_agent_ids={agent_id},
            registered_harness_identities={HarnessKey(agent_id, HarnessKind.CLAUDE_CODE)},
            profile_harness_kinds={"pinned": {HarnessKind.CLAUDE_CODE}, "current": {HarnessKind.CLAUDE_CODE}},
        )
    )
    runtimes = configured_runtimes(recording_claims)
    store = Store(migrated_sessions)
    service = SessionService(runtimes, store, session_wakes, launch_authorizer=authorizer)

    first = await service.create(operator_id, agent_id=agent_id, harness_kind=HarnessKind.CLAUDE_CODE)
    conversation_id = await store.conversation_of(first.session_id)
    async with migrated_sessions.begin() as db:
        agent = await db.get(Agent, agent_id)
        assert agent is not None
        agent.access_profile_id = "current"
    await store.fail(first.session_id, "replace this session")

    second = await service.create(operator_id, conversation_id=conversation_id)

    async with migrated_sessions() as db:
        conversation = await db.get(Conversation, conversation_id)
        replacement = await db.get(Session, second.session_id)
    assert conversation is not None
    assert replacement is not None
    assert (conversation.agent_id, conversation.access_profile_id, conversation.harness_kind) == (
        agent_id,
        "pinned",
        HarnessKind.CLAUDE_CODE,
    )
    assert replacement.conversation_id == conversation_id
    assert (replacement.operator_id, replacement.session_id) == (operator_id, second.session_id)
    assert [profile for _agent, profile, _db, _active in authorizer.calls] == [None, "pinned"]
    assert [active for _agent, _profile, _db, active in authorizer.calls] == [True, True]


def _console_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "harnesses": {"claude_code": runtime_config().model_dump(mode="json")},
        "auto_approval_policies": [{"id": "manual", "type": "never"}],
        "access_profiles": [{"id": "manual", "auto_approval_policy": "manual", "allowed_harnesses": ["claude_code"]}],
        "default_access_profile_id": "manual",
        "static_agents": [
            {
                "agent_id": "00000000-0000-4000-8000-000000000001",
                "display_name": "Console Agent",
                "token_env_var": "TOKEN",
                "operator_subject_env": "OPERATOR_SUBJECT",
                "access_profile_id": "manual",
            }
        ],
        "launchable_agents": [
            {"agent_id": "00000000-0000-4000-8000-000000000001", "system_prompt_template": "/prompt"}
        ],
    }
    config.update(overrides)
    return config


def test_chat_runtime_config_is_closed_and_rejects_the_retired_shape() -> None:
    parsed = ConsoleConfigFile.model_validate(_console_config())
    assert parsed.harnesses is not None
    assert parsed.harnesses.claude_code == runtime_config()

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ConsoleConfigFile.model_validate(
            _console_config(
                harnesses={
                    "claude_code": runtime_config().model_dump(mode="json"),
                    "future_runtime": runtime_config().model_dump(mode="json"),
                }
            )
        )

    with pytest.raises(ValidationError, match="claude_code must select the claude_code implementation"):
        ConsoleConfigFile.model_validate(
            _console_config(harnesses={"claude_code": _codex_runtime_config().model_dump(mode="json")})
        )

    with pytest.raises(ValidationError, match="claude_runtime was replaced"):
        ConsoleConfigFile.model_validate(_console_config(claude_runtime=runtime_config().model_dump(mode="json")))

    assert ConsoleConfigFile.model_validate(_console_config(harnesses=None)).harnesses is None


def test_chat_runtime_config_fails_closed_when_malformed() -> None:
    malformed = runtime_config().model_dump(mode="json")
    del malformed["namespace"]
    with pytest.raises(ValidationError, match="namespace"):
        ConsoleConfigFile.model_validate(_console_config(harnesses={"claude_code": malformed}))

    credentialed = runtime_config().model_dump(mode="json")
    credentialed["mcp_url"] = "https://session-secret@console.example/mcp"
    with pytest.raises(ValidationError, match="mcp_url"):
        ConsoleConfigFile.model_validate(_console_config(harnesses={"claude_code": credentialed}))

    flat = runtime_config().model_dump(mode="json")
    flat["auth_token_placeholder"] = flat.pop("implementation")["auth_token_placeholder"]
    flat["mcp_static_agent_id"] = flat.pop("agent_id")
    flat.pop("claim_prefix")
    flat.pop("runtime_label")
    with pytest.raises(ValidationError, match="implementation"):
        ConsoleConfigFile.model_validate(_console_config(harnesses={"claude_code": flat}))


def test_claude_environment_contains_placeholder_proxy_and_ca_only() -> None:
    config = runtime_config(ca_bundle="/ca/bundle.pem")

    assert config.environment() == {
        "ANTHROPIC_BASE_URL": "http://litellm.test:4000",
        "ANTHROPIC_AUTH_TOKEN": "not-a-secret",
        "ANTHROPIC_MODEL": "anthropic-max20/ant-messages/claude-sonnet-5",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic-max20/ant-messages/claude-haiku-4-5-20251001",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        "HTTP_PROXY": "http://proxy.test:8180",
        "HTTPS_PROXY": "http://proxy.test:8180",
        "NO_PROXY": "127.0.0.1,localhost,.svc,.svc.cluster.local,kubernetes.default.svc,10.0.0.0/8",
        "NODE_USE_ENV_PROXY": "1",
        "NODE_EXTRA_CA_CERTS": "/ca/bundle.pem",
        "SSL_CERT_FILE": "/ca/bundle.pem",
        "CURL_CA_BUNDLE": "/ca/bundle.pem",
        "REQUESTS_CA_BUNDLE": "/ca/bundle.pem",
    }


def _codex_runtime_config(**overrides: Any) -> RuntimeRegistrationConfig:
    implementation: dict[str, Any] = {
        "kind": "codex_app_server",
        "model": "codex-gpt-5.6-sol",
        "provider_id": "haku",
        "provider_name": "Haku OpenAI-compatible",
        "api_base_url": "http://litellm.litellm.svc.cluster.local:4000/v1",
        "api_key_env_var": "OPENAI_API_KEY",
        "github_token_placeholder": "proxy-github-placeholder",
    }
    for field in tuple(overrides):
        if field in implementation:
            implementation[field] = overrides.pop(field)
    values: dict[str, Any] = {
        "agent_id": "00000000-0000-4000-8000-000000000002",
        "namespace": "haku-runtime-sandbox",
        "warm_pool": "haku-public-coder-codex",
        "claim_prefix": "codex",
        "runtime_label": "codex-chat",
        "cwd": "/workspace",
        "session_ttl_seconds": 7200,
        "https_proxy": "http://public-coder-codex-runner-proxy:8080",
        "ca_bundle": "/ca/bundle.pem",
        "no_proxy": ".svc,.svc.cluster.local",
        "mcp_url": "http://haku-console:9090/mcp",
        "implementation": implementation,
    }
    values.update(overrides)
    return RuntimeRegistrationConfig(**values)


def test_codex_environment_keeps_provider_auth_in_the_sandbox_template() -> None:
    config = _codex_runtime_config()

    assert isinstance(config.implementation, CodexAppServerImplementationConfig)
    assert config.environment() == {
        "HTTP_PROXY": "http://public-coder-codex-runner-proxy:8080",
        "HTTPS_PROXY": "http://public-coder-codex-runner-proxy:8080",
        "NO_PROXY": ".svc,.svc.cluster.local",
        "NODE_USE_ENV_PROXY": "1",
        "NODE_EXTRA_CA_CERTS": "/ca/bundle.pem",
        "SSL_CERT_FILE": "/ca/bundle.pem",
        "CURL_CA_BUNDLE": "/ca/bundle.pem",
        "REQUESTS_CA_BUNDLE": "/ca/bundle.pem",
        "PIP_CERT": "/ca/bundle.pem",
        "GH_PAT": "proxy-github-placeholder",
        "GITHUB_TOKEN": "proxy-github-placeholder",
    }
    assert "OPENAI_API_KEY" not in config.environment()


def test_runtime_registration_threads_the_codex_model_and_effort_into_the_launch(
    recording_claims: RecordingClaims,
) -> None:
    # The console reads implementation.model/reasoning_effort into the launch environment the runner
    # reads for thread/start (haku/runner/codex/test_harness.py) -- the missing frame that let a codex
    # session fall back to its bare sandbox default and 403 at LiteLLM.
    config = _codex_runtime_config()
    assert isinstance(config.implementation, CodexAppServerImplementationConfig)
    registration = runtime_registration(config, recording_claims, system_prompt=SystemPromptTemplate(""))

    launch = registration.adapter.build_launch(
        RuntimeLaunch(cwd="/workspace", environment={}, mcp_servers={}, appended_system_prompt=None, resume_from=None)
    )

    assert launch.environment[CODEX_MODEL_ENV] == config.implementation.model
    assert launch.environment[CODEX_REASONING_EFFORT_ENV] == config.implementation.reasoning_effort


def test_claude_registration_uses_the_shared_discriminated_model() -> None:
    config = runtime_config(ca_bundle="/ca/bundle.pem")
    wire = config.model_dump(mode="json")

    assert set(wire) == {
        "agent_id",
        "namespace",
        "warm_pool",
        "claim_prefix",
        "runtime_label",
        "cwd",
        "session_ttl_seconds",
        "https_proxy",
        "ca_bundle",
        "no_proxy",
        "mcp_url",
        "implementation",
    }
    assert wire["implementation"] == {
        "kind": "claude_code",
        "api_base_url": "http://litellm.test:4000",
        "model": "anthropic-max20/ant-messages/claude-sonnet-5",
        "haiku_model": "anthropic-max20/ant-messages/claude-haiku-4-5-20251001",
        "auth_token_placeholder": "not-a-secret",
        "gateway_discovery": True,
    }
    assert RuntimeRegistrationConfig.model_validate(wire) == config
    assert isinstance(config.implementation, ClaudeCodeImplementationConfig)
    assert config.kind is HarnessKind.CLAUDE_CODE
    assert (config.agent_id, config.claim_prefix, config.runtime_label) == (
        UUID("00000000-0000-4000-8000-000000000001"),
        "claude",
        "claude-chat",
    )


def test_runtime_registration_requires_an_explicit_implementation_discriminator() -> None:
    raw = _codex_runtime_config().model_dump(mode="json")
    implementation = raw["implementation"]
    assert isinstance(implementation, dict)
    implementation.pop("kind")

    with pytest.raises(ValidationError, match="union_tag_not_found"):
        RuntimeRegistrationConfig.model_validate(raw)


def test_runtime_registration_schema_exposes_the_implementation_discriminator() -> None:
    schema = RuntimeRegistrationConfig.model_json_schema()
    implementation = schema["properties"]["implementation"]
    if reference := implementation.get("$ref"):
        implementation = schema["$defs"][reference.rsplit("/", 1)[-1]]

    assert implementation["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "claude_code": "#/$defs/ClaudeCodeImplementationConfig",
            "codex_app_server": "#/$defs/CodexAppServerImplementationConfig",
        },
    }
    assert len(implementation["oneOf"]) == 2


def test_codex_runtime_rejects_session_authority_as_the_provider_key() -> None:
    with pytest.raises(ValidationError, match="exact-session credential"):
        _codex_runtime_config(api_key_env_var="HAKU_RUNNER_TOKEN")


@pytest.mark.parametrize("field", ["api_base_url", "mcp_url"])
def test_runtime_registration_rejects_credentials_in_control_plane_urls(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _codex_runtime_config(**{field: "http://durable-secret@example.test/path"})


async def _allocated_session(chat_service: SessionService, recording_claims: RecordingClaims, operator_id: UUID):
    """Seed a claim-backed session for tests whose subject starts after allocation."""
    view, token = await chat_service._store._create_provisioning_for_test(
        operator_id,
        agent_id=TEST_AGENT_ID,
        access_profile_id=TEST_ACCESS_PROFILE_ID,
        harness_kind=HarnessKind.CLAUDE_CODE,
    )
    await recording_claims.create(
        session_id=view.session_id, bridge_token=token, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    return view


async def test_the_first_idle_prompt_creates_the_claim_once(
    allocator, session_store, chat_service, recording_claims, operator_id
) -> None:
    session = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)
    assert await session_store.status(session.session_id) == SessionStatus.IDLE
    assert recording_claims.created == []

    item_id = await chat_service.enqueue_prompt(operator_id, session.session_id, "start now", SPA_ORIGIN)
    assert await session_store.status(session.session_id) == SessionStatus.IDLE
    assert recording_claims.created == []

    await allocator.allocate_once()
    allocated_again = await chat_service.allocate(operator_id, session.session_id)

    assert item_id is not None
    assert allocated_again is False
    assert recording_claims.created == [session.session_id]
    assert session.session_id in recording_claims.tokens
    assert await session_store.status(session.session_id) == SessionStatus.PROVISIONING


async def test_idle_provisioning_details_do_not_read_kubernetes(chat_service, recording_claims, operator_id) -> None:
    session = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)

    view = await chat_service.sandbox_provisioning(operator_id, session.session_id)

    assert view.status == SessionStatus.IDLE
    assert view.sandbox is None
    assert recording_claims.inspected == []


async def test_a_returning_runner_is_admitted_and_takes_the_lease(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """A runner whose replica went away is admitted by the next one, which keeps the sandbox."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    token = recording_claims.tokens[session_id]
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED

    with patch("haku.console.session.store.REPLICA", "haku-console-b"):
        assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.HELD, (
            "a replica still renewing its lease keeps the session it is serving — but only until it lapses"
        )
        await session_store.release_lease(session_id)
        assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED, (
            "a session handed back is adoptable by whichever replica the runner reaches"
        )


async def test_startup_reconciliation_retries_terminal_claim_cleanup(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """Claims left behind by a Console that died mid-teardown are swept on the next boot."""

    session_ids = []
    for _ in range(2):
        session = await _allocated_session(chat_service, recording_claims, operator_id)
        await session_store.request_close(operator_id, session.session_id)
        session_ids.append(session.session_id)

    await chat_service.reconcile_terminal_claims()

    assert sorted(recording_claims.deleted) == sorted(session_ids)
    assert await session_store.claim_cleanup_candidates() == []


ROOM = "!room:example.org"


async def test_only_an_attached_chat_conversation_gets_the_chat_prompt(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """The conversation selects chat context; the session never receives a channel object."""
    service = SessionService(
        configured_runtimes(
            recording_claims, system_prompt=SystemPromptTemplate("{{ session_id }} {{ recent_messages | length }}")
        ),
        session_store,
        session_wakes,
        conversation_history=ConversationHistory(migrated_sessions),
    )
    spa, _ = await session_store.create(
        operator_id,
        agent_id=TEST_AGENT_ID,
        access_profile_id=TEST_ACCESS_PROFILE_ID,
        harness_kind=HarnessKind.CLAUDE_CODE,
    )
    attached, _ = await session_store.create(
        operator_id,
        agent_id=TEST_AGENT_ID,
        access_profile_id=TEST_ACCESS_PROFILE_ID,
        harness_kind=HarnessKind.CLAUDE_CODE,
    )
    await attach_channel(migrated_sessions, attached.session_id, ROOM)

    assert await service._appended_prompt(spa.session_id) is None
    assert await service._appended_prompt(attached.session_id) == f"{attached.session_id} 0"


async def test_a_returning_runner_beats_the_sweep(
    session_store, chat_service, recording_claims, operator_id, migrated_sessions
) -> None:
    """A runner that redials inside the adoption window is admitted, and the session keeps running
    under its new holder rather than being failed."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    token = recording_claims.tokens[session_id]
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await age_lease(migrated_sessions, session_id, seconds_ago=1)

    with patch("haku.console.session.store.REPLICA", "haku-console-b"):
        assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED, (
            "a lapsed lease is adoptable by whichever replica the runner reaches"
        )

    assert await session_store.expire_stale_leases() == 0
    assert await session_store.status(session_id) in OPEN_SESSION_STATUSES


async def test_the_lease_heartbeat_also_slides_the_sandbox_deadline(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """The sandbox is a renewed lease, not a fixed timer: the heartbeat that renews the console
    lease also pushes the SandboxClaim's deadline out, so an active session is not reaped at
    `session_ttl_seconds`."""
    view, token = await session_store.create(
        operator_id,
        agent_id=TEST_AGENT_ID,
        access_profile_id=TEST_ACCESS_PROFILE_ID,
        harness_kind=HarnessKind.CLAUDE_CODE,
    )
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    heartbeat = asyncio.create_task(chat_service._renew_lease(view.session_id))
    try:
        for _ in range(200):
            if recording_claims.renewed:
                break
            await asyncio.sleep(0.01)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat

    assert recording_claims.renewed, "the heartbeat slid no sandbox deadline"
    session_id, expires_at = recording_claims.renewed[0]
    assert session_id == view.session_id
    assert expires_at > datetime.now(UTC)


async def test_a_released_session_nobody_readopted_is_not_called_never_attached(
    session_store, recording_claims, chat_service, migrated_sessions, operator_id
) -> None:
    """A runner attached, then its lease was handed back (a roll, or the sandbox reaching its TTL)
    and no runner returned. `release` clears `lease_holder`, so the reason must not fall through to
    "never attached" for a session that was attached for hours."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    token = recording_claims.tokens[session.session_id]
    assert await session_store.authenticate_bridge(session.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.release_lease(session.session_id)
    await age_lease(migrated_sessions, session.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await session_store.expire_stale_leases() == 1
    error = (await session_store.get(operator_id, session.session_id)).error
    assert "never attached" not in error, "it was attached; the runner went away"
    assert "runner went away" in error


async def test_a_session_that_failed_to_come_up_still_says_what_it_was_stuck_behind(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """The reason to ask a session that is no longer provisioning: the conversation read answers
    `null` for a failed session, which is the one that most needs to be asked why."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    recording_claims.answer(
        provisioning_view(
            f"claude-{session.session_id.hex}",
            step=ProvisioningStep.WAITING_FOR_POD,
            claim_ready=False,
            claim_message="no warm sandbox available",
        )
    )
    await session_store.fail(session.session_id, "sandbox provisioning failed")

    view = await chat_service.sandbox_provisioning(operator_id, session.session_id)

    assert view.status is SessionStatus.FAILED
    assert view.harness_kind == "claude_code"
    assert view.sandbox.step is ProvisioningStep.WAITING_FOR_POD
    assert view.sandbox.claim_message == "no warm sandbox available"


async def test_a_reclaimed_claim_is_reported_as_gone_rather_than_as_nothing(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """`_cleanup_terminal_claim` deletes the claim once a session ends, so the cluster has nothing
    to show — a claim that is gone being a fact rather than an absence of one."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    recording_claims.answer(provisioning_view(f"claude-{session.session_id.hex}", step=ProvisioningStep.CLAIM_ABSENT))
    await session_store.closed(session.session_id)

    view = await chat_service.sandbox_provisioning(operator_id, session.session_id)

    assert view.status is SessionStatus.CLOSED
    assert view.sandbox.step is ProvisioningStep.CLAIM_ABSENT


async def test_a_cluster_that_cannot_be_read_says_so_instead_of_failing_the_request(
    chat_service, recording_claims, operator_id
) -> None:
    """The other answer a reader must tell apart from "the claim is gone": "I could not look" — the
    one it must not act on."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    recording_claims.fail(RuntimeError("kubernetes: connection refused"))

    view = await chat_service.sandbox_provisioning(operator_id, session.session_id)

    assert view.sandbox.observation_error == "kubernetes: connection refused"


async def test_polling_provisioning_reads_the_cluster_at_a_bounded_rate(
    chat_service, recording_claims, operator_id
) -> None:
    """One poll is up to three Kubernetes reads, and the browser's refresh rate is not the API
    server's problem — so polls inside one observation's budget cost one look at the cluster."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)

    with patch("haku.console.session.runtime.OBSERVATION_TTL", timedelta(hours=1)):
        for _ in range(5):
            await chat_service.sandbox_provisioning(operator_id, session.session_id)
    assert recording_claims.inspected == [session.session_id]

    with patch("haku.console.session.runtime.OBSERVATION_TTL", timedelta(0)):
        await chat_service.sandbox_provisioning(operator_id, session.session_id)
    assert recording_claims.inspected == [session.session_id] * 2, (
        "a view past its budget is taken again rather than served stale"
    )


async def test_provisioning_is_not_readable_for_a_session_another_operator_owns(chat_service, operator_id) -> None:
    session = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)

    with pytest.raises(KeyError):
        await chat_service.sandbox_provisioning(uuid4(), session.session_id)


async def test_transient_database_error_recognizes_a_real_postgres_deadlock(
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The predicate must match what SQLAlchemy's asyncpg dialect actually raises for SQLSTATE
    40P01, not a hand-built stand-in — so this manufactures a genuine deadlock: two transactions
    take two advisory xact locks in opposite orders, a barrier holding both first locks until both
    are held."""
    barrier = asyncio.Barrier(2)

    async def cross_lock(first: int, second: int) -> None:
        async with migrated_sessions.begin() as db:
            await db.execute(select(func.pg_advisory_xact_lock(first)))
            await barrier.wait()
            await db.execute(select(func.pg_advisory_xact_lock(second)))

    outcomes = await asyncio.gather(cross_lock(1, 2), cross_lock(2, 1), return_exceptions=True)
    error = one(outcome for outcome in outcomes if isinstance(outcome, BaseException))
    assert _transient_database_error(error)


async def test_transient_database_error_rejects_an_integrity_error(
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A constraint violation fails identically on retry, so it is the turn's own failure."""
    with pytest.raises(IntegrityError) as excinfo:
        async with migrated_sessions.begin() as db:
            db.add(
                Conversation(
                    conversation_id=uuid4(),
                    # References no operator row, so the INSERT is a foreign-key violation.
                    operator_id=uuid4(),
                    harness_kind=HarnessKind.CLAUDE_CODE,
                    created_at=datetime.now(UTC),
                )
            )
    assert not _transient_database_error(excinfo.value)


if __name__ == "__main__":
    pytest_bazel.main()
