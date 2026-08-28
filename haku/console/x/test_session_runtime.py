"""Focused contracts for the Agent Sandbox Claude chat runtime.

**No channel is imported here, deliberately.** An attachment only selects whether the conversation
gets the shared direct-chat system prompt; setup, answers, silence and live state are durable facts
that channel subscribers project. What a homeserver's messages become is
<channels/matrix/test_conversation.py>, beside the `Turns` that makes them turns.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastapi import HTTPException
from more_itertools import one
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.agents.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.chat_models import (
    OPEN_SESSION_STATUSES,
    SPA_ORIGIN,
    BridgeFrameKind,
    FrameDirection,
    ItemStatus,
    ItemType,
    PromptOriginKind,
    RuntimeKind,
    SessionStatus,
    ToolOutcome,
    TurnOutcome,
)
from haku.console.config import ChatRuntimesConfig, ClaudeCodeImplementationConfig, RuntimeRegistrationConfig
from haku.console.conftest import console_sessions
from haku.console.conversation_read_access import UnrestrictedReads
from haku.console.database_schema import (
    Agent,
    Conversation,
    ConversationEventRow,
    ConversationItem,
    ConversationTurn,
    Session,
    SessionFrame,
)
from haku.console.mcp_config import ConsoleConfigFile
from haku.console.x import reprojection
from haku.console.x.claude_code.client import ClaudeCli
from haku.console.x.claude_code.frames import DELTA_FRAME_KIND
from haku.console.x.claude_code.runtime import ClaudeRuntimeAdapter
from haku.console.x.claude_code.testing.wire import (
    assistant,
    content_block_stop,
    input_json_delta,
    prompt,
    result,
    system,
    text_block,
    text_delta,
    thinking_block,
    tool_result,
    tool_use_block,
    tool_use_start,
)
from haku.console.x.codex_app_server.config import CodexAppServerImplementationConfig
from haku.console.x.conftest import (
    age_lease,
    answers,
    attach_channel,
    configured_runtimes,
    lease_of,
    runtime_config,
    session_items,
)
from haku.console.x.conversation_events import (
    CallRef,
    ConversationEvent as NeutralConversationEvent,
    FrameRange,
    ItemSegment,
    MessageCompleted,
    MessageStarted,
    OpenRef,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnAnswered,
    TurnCompleted,
    TurnFailed,
)
from haku.console.x.conversation_history import ConversationHistory
from haku.console.x.conversation_reads import PromptEntry, TurnAnsweredEnd, TurnFailedEnd
from haku.console.x.item_entries import entry_of
from haku.console.x.launch_identity import ChatLaunchAuthorizer, LaunchAgentRejectedError, LaunchIdentity
from haku.console.x.runtime import (
    EMPTY_TURN_PROJECTION_SEED,
    Checkpoint,
    FrameEffects,
    OpenItemSeed,
    RuntimeKey,
    RuntimeRegistry,
    RuntimeUnusable,
    TurnCompletion,
    TurnProjectionSeed,
)
from haku.console.x.sandbox_claims import ProvisioningStep, provisioning_view
from haku.console.x.session_runtime import (
    GOING_AWAY_CODE,
    ConversationCreateRequest,
    RolloutRecorder,
    SessionService,
    _inherited,
    _replaying,
    _transient_database_error,
    create_conversation,
)
from haku.console.x.session_store import ADOPTION_GRACE, BridgeAuthentication, SessionStore
from haku.console.x.session_wakes import SessionWakes
from haku.console.x.system_prompt import SystemPromptTemplate
from haku.console.x.testing.recording_claims import RecordingClaims
from haku.runtime.x.bridge.client import FrameSink, ReceivedFrame, SentPrompt
from haku.runtime.x.bridge.protocol import NOT_ADMITTED_CODE, HarnessFrame


def test_runtime_deployment_wiring_has_no_application_defaults() -> None:
    assert all(field.is_required() for field in RuntimeRegistrationConfig.model_fields.values())
    assert ChatRuntimesConfig.model_fields["claude_code"].is_required()
    assert not ChatRuntimesConfig.model_fields["codex_app_server"].is_required()
    assert not ConsoleConfigFile.model_fields["harnesses"].is_required()


def test_new_conversation_request_rejects_client_supplied_access_profile() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ConversationCreateRequest.model_validate(
            {"agent_id": str(uuid4()), "runtime": RuntimeKind.CLAUDE_CODE, "access_profile_id": "admin"}
        )


@pytest.mark.parametrize("body", [{"agent_id": str(uuid4())}, {"runtime": RuntimeKind.CLAUDE_CODE}])
def test_new_conversation_request_requires_the_complete_launch_pair(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Field required"):
        ConversationCreateRequest.model_validate(body)


async def test_post_conversation_launch_rejection_is_generic_403() -> None:
    class RejectingService:
        async def create_conversation(self, *_args: Any, **_kwargs: Any) -> None:
            raise LaunchAgentRejectedError("durable internal reason")

    actor = type("Actor", (), {"operator_id": uuid4()})()
    with pytest.raises(HTTPException) as error:
        await create_conversation(
            ConversationCreateRequest(agent_id=uuid4(), runtime=RuntimeKind.CLAUDE_CODE),
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
        runtime_kind: RuntimeKind,
        *,
        expected_profile_id: str | None = None,
    ) -> LaunchIdentity:
        assert db.in_transaction()
        self.calls.append((agent_id, expected_profile_id, db, db.in_transaction()))
        return await self._delegate(db, operator_id, agent_id, runtime_kind, expected_profile_id=expected_profile_id)


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
            registered_runtime_identities={RuntimeKey(agent_id, RuntimeKind.CLAUDE_CODE)},
            profile_runtime_kinds={"pinned": {RuntimeKind.CLAUDE_CODE}, "current": {RuntimeKind.CLAUDE_CODE}},
        )
    )
    runtimes = configured_runtimes(recording_claims)
    store = SessionStore(migrated_sessions, runtimes)
    service = SessionService(runtimes, store, session_wakes, launch_authorizer=authorizer, default_agent_id=agent_id)

    first = await service.create(operator_id)
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
    assert (conversation.agent_id, conversation.access_profile_id, conversation.runtime_kind) == (
        agent_id,
        "pinned",
        RuntimeKind.CLAUDE_CODE,
    )
    assert replacement.conversation_id == conversation_id
    assert (replacement.operator_id, replacement.session_id) == (operator_id, second.session_id)
    assert [profile for _agent, profile, _db, _active in authorizer.calls] == [None, "pinned"]
    assert [active for _agent, _profile, _db, active in authorizer.calls] == [True, True]


def _console_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "harnesses": {"claude_code": runtime_config().model_dump(mode="json")},
        "auto_approval_policies": [{"id": "manual", "type": "never"}],
        "access_profiles": [
            {"id": "manual", "auto_approval_policy": "manual", "allowed_chat_runtimes": ["claude_code"]}
        ],
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
        "default_chat_agent_id": "00000000-0000-4000-8000-000000000001",
    }
    config.update(overrides)
    return config


def test_chat_runtime_config_is_closed_and_rejects_the_retired_shape() -> None:
    parsed = ConsoleConfigFile.model_validate(_console_config())
    assert parsed.harnesses is not None
    assert parsed.harnesses.claude_code == runtime_config()

    old_shared_config = _console_config()
    old_shared_config.pop("launchable_agents")
    old_shared_config.pop("default_chat_agent_id")
    with pytest.raises(ValidationError, match="default chat Agent"):
        ConsoleConfigFile.model_validate(old_shared_config)

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
    flat["oauth_placeholder"] = flat.pop("implementation")["oauth_placeholder"]
    flat["mcp_static_agent_id"] = flat.pop("agent_id")
    flat.pop("claim_prefix")
    flat.pop("runtime_label")
    with pytest.raises(ValidationError, match="implementation"):
        ConsoleConfigFile.model_validate(_console_config(harnesses={"claude_code": flat}))


def test_claude_environment_contains_placeholder_proxy_and_ca_only() -> None:
    config = runtime_config(ca_bundle="/ca/bundle.pem")

    assert config.environment() == {
        "CLAUDE_CODE_OAUTH_TOKEN": "not-a-secret",
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
    assert wire["implementation"] == {"kind": "claude_code", "oauth_placeholder": "not-a-secret"}
    assert RuntimeRegistrationConfig.model_validate(wire) == config
    assert isinstance(config.implementation, ClaudeCodeImplementationConfig)
    assert config.kind is RuntimeKind.CLAUDE_CODE
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
        _codex_runtime_config(api_key_env_var="HAKU_AGENT_SDK_RUNNER_TOKEN")


@pytest.mark.parametrize("field", ["api_base_url", "mcp_url"])
def test_runtime_registration_rejects_credentials_in_control_plane_urls(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _codex_runtime_config(**{field: "http://durable-secret@example.test/path"})


# The gap this double leaves between one frame's number and the next. Deliberately not 1:
# `session_frames.frame_seq` is a Postgres `Identity` column, so the real sequence has gaps and
# nothing may read one as a frame that went missing.
_FAKE_SEQ_STRIDE = 5


class _FakeCli:
    """A `ClaudeCli` that replays scripted frames — frames rather than SDK objects, as the runtime
    consumes them, so the double cannot drift from the wire."""

    def __init__(
        self,
        script: list[dict[str, Any]] | None = None,
        *,
        frame_seqs: Sequence[int] | None = None,
        prompt_frame_seq: int | None = None,
    ):
        self.script = list(script or [])
        # What the rollout numbered each scripted frame, for the tests that assert a projection
        # points back at one. A test with nothing to say about provenance passes neither and this
        # double numbers for itself, since every frame the real client hands on carries a number.
        self._next_seq = _FAKE_SEQ_STRIDE
        self.frame_seqs = frame_seqs
        self.prompt_frame_seq = self._number() if prompt_frame_seq is None else prompt_frame_seq
        self.prompts: list[str] = []
        self.interrupted = False
        self.closed = False
        self._queue: asyncio.Queue[ReceivedFrame] = asyncio.Queue()
        self._disconnected = asyncio.Event()

    async def connect(self) -> dict[str, Any]:
        return {"subtype": "success"}

    async def query(self, text: str) -> SentPrompt:
        self.prompts.append(text)
        self.replay()
        return SentPrompt(frame_seq=self.prompt_frame_seq)

    def replay(self) -> None:
        """Deliver the script with nothing having been asked, as the runner's replay window does.

        A resumed turn asks no question — its question was asked by a process that is gone — so a
        double that only speaks when spoken to could not stand in for one.
        """
        for frame_seq, frame in zip(self.frame_seqs or [self._number() for _ in self.script], self.script, strict=True):
            self.deliver(frame, frame_seq)

    def deliver(self, frame: dict[str, Any], frame_seq: int | None = None) -> None:
        self._queue.put_nowait(
            ReceivedFrame(
                envelope=HarnessFrame(frame=frame), frame_seq=self._number() if frame_seq is None else frame_seq
            )
        )

    def _number(self) -> int:
        seq = self._next_seq
        self._next_seq += _FAKE_SEQ_STRIDE
        return seq

    async def interrupt(self) -> None:
        self.interrupted = True

    async def wait_closed(self) -> None:
        # A healthy fake stream never ends on its own; a test that wants to model the socket
        # dropping calls `disconnect()`, as the real reader's end sets the real event.
        await self._disconnected.wait()

    def disconnect(self) -> None:
        self._disconnected.set()

    async def frames(self):
        # Never ends on its own: a real CLI stays open between turns, and a generator that
        # stopped after the first `result` would make the second turn look like a dead stream.
        while True:
            yield await self._queue.get()

    async def aclose(self) -> None:
        self.closed = True


class _UnrelatedTurnHandler:
    """A stateful native fold whose wire shares no vocabulary with Claude or JSON-RPC."""

    def __init__(self, seed: TurnProjectionSeed):
        self._opened_at = None if seed.open_message is None else seed.open_message.first_frame_seq
        self._last = None if seed.open_message is None else seed.open_message.last_frame_seq

    def apply(self, *, frame_seq: int, frame: HarnessFrame) -> FrameEffects:
        payload = frame.frame
        if payload.get("阶段") == "碎片":
            provenance = FrameRange(frame_seq, frame_seq)
            fragment_events: list[NeutralConversationEvent] = []
            if self._opened_at is None:
                self._opened_at = frame_seq
                fragment_events.append(MessageStarted(provenance=provenance))
            self._last = frame_seq
            fragment_events.append(
                ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text=str(payload["正文"]), provenance=provenance)
            )
            return FrameEffects(events=tuple(fragment_events))
        if payload.get("阶段") == "最终":
            provenance = FrameRange(frame_seq, frame_seq)
            final_events: list[NeutralConversationEvent] = []
            if self._opened_at is None and payload.get("正文"):
                final_events.extend(
                    (
                        MessageStarted(provenance=provenance),
                        ItemSegment(
                            item=OpenRef(item_type=ItemType.MESSAGE), text=str(payload["正文"]), provenance=provenance
                        ),
                    )
                )
            if tail := payload.get("尾声"):
                final_events.append(
                    ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text=str(tail), provenance=provenance)
                )
            if self._opened_at is not None or payload.get("正文"):
                final_events.append(MessageCompleted(backend_item_id=None, provenance=provenance))
            final_events.append(TurnCompleted(end=TurnAnswered(), provenance=provenance))
            return FrameEffects(
                events=tuple(final_events),
                completion=TurnCompletion(end=TurnAnswered(), final_text=str(payload.get("正文") or "").strip()),
            )
        return FrameEffects()


class _UnrelatedRuntimeAdapter:
    kind = RuntimeKind.CLAUDE_CODE
    display_name = "Unrelated test harness"

    def turn_handler(self, seed: TurnProjectionSeed = EMPTY_TURN_PROJECTION_SEED) -> _UnrelatedTurnHandler:
        return _UnrelatedTurnHandler(seed)

    def prompt_submitted(self, frames: Iterable[HarnessFrame]) -> bool:
        return any(frame.frame.get("动作") == "输入" for frame in frames)

    def wake_watcher(self) -> None:
        return None

    def build_launch(self, launch):
        raise AssertionError("this projection-only test must not build a runner launch")

    def client(self, websocket, launch, progress, frames_to):
        raise AssertionError("this projection-only test must not construct a runner client")


class _GivingUpTurnHandler:
    """Fails its turn, and says whether the runtime can still serve another."""

    def __init__(self, *, unusable: bool) -> None:
        self._unusable = unusable

    def apply(self, *, frame_seq: int, frame: HarnessFrame) -> FrameEffects:
        end = TurnFailed(reason="the provider gave up")
        return FrameEffects(
            events=(TurnCompleted(end=end, provenance=FrameRange(frame_seq, frame_seq)),),
            completion=TurnCompletion(end=end, final_text=""),
            unusable=RuntimeUnusable(reason="the thread reported a system error") if self._unusable else None,
        )


class _GivingUpRuntimeAdapter(_UnrelatedRuntimeAdapter):
    def __init__(self, *, unusable: bool) -> None:
        self._unusable = unusable

    def turn_handler(self, seed: TurnProjectionSeed = EMPTY_TURN_PROJECTION_SEED) -> Any:
        return _GivingUpTurnHandler(unusable=self._unusable)


async def _one_failed_turn(session_store, session_wakes, operator_id, *, unusable: bool) -> UUID:
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "go", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    runtimes = RuntimeRegistry({RuntimeKind.CLAUDE_CODE: _GivingUpRuntimeAdapter(unusable=unusable)})
    service = SessionService(runtimes, session_store, session_wakes)
    client = _FakeCli([{"done": True}])
    await service._run_turn(client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event())
    session_id: UUID = view.session_id
    return session_id


async def test_a_failed_turn_alone_leaves_the_session_usable(session_store, session_wakes, operator_id) -> None:
    """#4752: the exchange failing is not the session failing.

    The turn closes with its reason, so the operator can read it and send another prompt. Only the
    runtime saying it can serve no other ends the session, which it says separately.
    """
    session_id = await _one_failed_turn(session_store, session_wakes, operator_id, unusable=False)

    [record] = await session_store.list_turns(str(session_id), cursor=None, limit=5, scope=UnrestrictedReads())
    assert record.end == TurnFailedEnd(failure="the provider gave up")
    assert await session_store.status(session_id) != SessionStatus.FAILED


async def test_a_runtime_that_declares_itself_unusable_ends_the_session(
    session_store, session_wakes, operator_id
) -> None:
    """The other half: when the runtime does say so, the session ends carrying the turn's reason."""
    with pytest.raises(RuntimeError, match="the provider gave up"):
        await _one_failed_turn(session_store, session_wakes, operator_id, unusable=True)


async def test_generic_turn_loop_is_opaque_to_a_discriminator_free_harness(
    session_store, migrated_sessions, session_wakes, operator_id
) -> None:
    """Unrelated keys survive dedup/storage while only the integration assigns semantics."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "say hello", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None

    recorder = RolloutRecorder(session_store, view.session_id)
    prompt_frame_seq = await recorder.sent(HarnessFrame(frame={"动作": "输入", "正文": "say hello"}, seq=10))
    native = [
        HarnessFrame(frame={"阶段": "碎片", "正文": "你"}, seq=11),
        HarnessFrame(frame={"阶段": "碎片", "正文": "好"}, seq=12),
        HarnessFrame(frame={"阶段": "最终", "正文": "你好!", "尾声": "!", "成功": True}, seq=13),
    ]
    recorded = [await recorder.received(frame) for frame in native]
    duplicate = await recorder.received(native[1])
    assert duplicate.frame_seq == recorded[1].frame_seq

    runtimes = RuntimeRegistry({RuntimeKind.CLAUDE_CODE: _UnrelatedRuntimeAdapter()})
    service = SessionService(runtimes, session_store, session_wakes)
    client = _FakeCli(
        [frame.frame for frame in native],
        frame_seqs=[frame.frame_seq for frame in recorded],
        prompt_frame_seq=prompt_frame_seq,
    )
    await service._run_turn(client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event())

    assert await answers(migrated_sessions, view.session_id) == ["你好!"]
    raw = await session_store.read_session_frames(view.session_id, cursor=None, limit=25, scope=UnrestrictedReads())
    assert [frame.payload for frame in raw] == [
        {"动作": "输入", "正文": "say hello"},
        {"阶段": "碎片", "正文": "你"},
        {"阶段": "碎片", "正文": "好"},
        {"阶段": "最终", "正文": "你好!", "尾声": "!", "成功": True},
    ]
    async with migrated_sessions() as db:
        report = await reprojection.check_session(db, view.session_id, runtimes=runtimes)
    assert one(report.turns).outcome == reprojection.Agrees()


_TOOL_USE_SCRIPT = [
    assistant(tool_use_block("toolu_01", "mcp__haku-console__haku-console__list_mcp_servers", {})),
    assistant(text_block("The Haku Console catalog is available.")),
    result(text="The Haku Console catalog is available."),
]


async def test_run_turn_preserves_assistant_message_boundaries_around_tool_use(
    session_store, chat_service, migrated_sessions, operator_id
) -> None:
    """A tool-use block and the text after it are two messages, not one merged row."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "Check the Haku MCP catalog", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None

    client = _FakeCli(_TOOL_USE_SCRIPT)
    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event()
    )

    items = [
        item
        for item in await session_items(migrated_sessions, view.session_id)
        if item.item_type is not ItemType.PROMPT
    ]
    # A call is a sibling of the message rather than a field on it, and it is `open` because no
    # `user` frame answered it in this test — which the row says, rather than showing an empty
    # result.
    assert [(item.item_type, item.item_text, item.tool_name, item.status) for item in items] == [
        (ItemType.TOOL_CALL, "", "mcp__haku-console__haku-console__list_mcp_servers", ItemStatus.OPEN),
        (ItemType.MESSAGE, "The Haku Console catalog is available.", None, ItemStatus.COMPLETE),
    ]
    assert await session_store.status(view.session_id) == SessionStatus.READY, "the turn was not completed"


async def test_run_turn_accepts_a_stream_only_tool_call_before_its_result(
    session_store, chat_service, migrated_sessions, operator_id
) -> None:
    """A result cannot outrun the streamed declaration that made its call addressable.

    Captured from the production failure on 2026-08-19: Claude Code 2.1.220 executed two parallel
    calls without first emitting their completed `assistant` blocks. The first result used to make
    `LogWriter` fail the whole session with "no call was asked under this id".
    """
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "update the notes", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None

    client = _FakeCli(
        [
            tool_use_start("toolu_01", "Bash", index=1),
            input_json_delta('{"command": "true"}', index=1),
            content_block_stop(index=1),
            tool_result("toolu_01", "ok", structured={"stdout": "ok"}, is_error=False),
            result(text="updated"),
        ]
    )
    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event()
    )

    call = one(
        item for item in await session_items(migrated_sessions, view.session_id) if item.item_type is ItemType.TOOL_CALL
    )
    assert (call.tool_name, call.arguments, call.item_text, call.outcome, call.status) == (
        "Bash",
        {"command": "true"},
        "ok",
        ToolOutcome.SUCCEEDED,
        ItemStatus.COMPLETE,
    )
    assert await session_store.status(view.session_id) == SessionStatus.READY


async def _open_items(sessions, session_id) -> list[UUID]:
    """The items this session has open, oldest first — what an adopting replica continues."""
    async with sessions() as db:
        return list(
            await db.scalars(
                select(ConversationItem.item_id)
                .where(ConversationItem.session_id == session_id, ConversationItem.status == ItemStatus.OPEN)
                .order_by(ConversationItem.opened_seq)
            )
        )


async def _frames_behind(sessions, item_id) -> tuple[int | None, int | None]:
    """The span of frames an item's own log rows were read from."""
    async with sessions() as db:
        rows = (
            await db.scalars(
                select(ConversationEventRow)
                .where(ConversationEventRow.item_id == item_id)
                .order_by(ConversationEventRow.event_seq)
            )
        ).all()
    firsts = [row.source_first_frame_seq for row in rows if row.source_first_frame_seq is not None]
    lasts = [row.source_last_frame_seq for row in rows if row.source_last_frame_seq is not None]
    return (min(firsts) if firsts else None, max(lasts) if lasts else None)


async def test_projected_assistant_message_points_to_the_frames_that_built_it(
    session_store, chat_service, operator_id, migrated_sessions
) -> None:
    """A message row keeps a navigable range into the lossless rollout rather than only a copy."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "say hello", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None

    # Recorded through the real sink, so the numbers the turn is handed are the rollout's own.
    script = [text_delta("hello"), assistant(text_block("hello")), result(text="hello")]
    recorder = RolloutRecorder(session_store, view.session_id)
    prompt_frame_seq = await recorder.sent(HarnessFrame(frame=prompt("say hello")))
    frame_seqs = [(await recorder.received(HarnessFrame(frame=frame))).frame_seq for frame in script]

    client = _FakeCli(script, frame_seqs=frame_seqs, prompt_frame_seq=prompt_frame_seq)
    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event()
    )

    items = await session_items(migrated_sessions, view.session_id)
    said = one(item for item in items if item.item_type is ItemType.MESSAGE)
    # **The item carries no frame numbers.** They are one session's coordinates, so what an operator
    # appeals to is the log rows that actually built the message: the delta that opened it and the
    # `assistant` frame that completed it. The later `result` closes the turn but adds no message
    # effect, so it does not widen this item's provenance.
    assert await _frames_behind(migrated_sessions, said.item_id) == (frame_seqs[0], frame_seqs[1])
    # A prompt is authored: it was accepted before anything crossed a wire, so it names no frames
    # at all rather than naming the one it went out as.
    asked = one(item for item in items if item.item_type is ItemType.PROMPT)
    assert await _frames_behind(migrated_sessions, asked.item_id) == (None, None)


class _LifecycleWebSocket:
    def __init__(self):
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.denied: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    async def send_denial_response(self, response: Any) -> None:
        """The ASGI `websocket.http.response` extension, which is how a handshake answers a status
        other than 403. Recorded rather than sent, since what matters here is *which* status."""
        self.denied = response.status_code


class _LifecycleClaudeClient(_FakeCli):
    """A `cli_over_websocket` stand-in that records through the sink it is handed.

    The sink is not optional in the real client (<claude_code/client.py>): every
    frame either way is written as it crosses the wire and numbered from the row it landed in, so a
    double that dropped it would hand the turn loop numbers naming no row.
    """

    last_launch: object | None = None

    def __init__(self, adapter: object, launch: object, on_progress: object, frames_to: FrameSink):
        super().__init__()
        type(self).last_launch = launch
        self._frames_to = frames_to
        self.connected = False

    async def connect(self) -> dict[str, Any]:
        self.connected = True
        return {"subtype": "success"}

    async def query(self, text: str) -> SentPrompt:
        self.prompt_frame_seq = await self._frames_to.sent(HarnessFrame(frame=prompt(text)))
        self.frame_seqs = [
            (await self._frames_to.received(HarnessFrame(frame=frame))).frame_seq for frame in self.script
        ]
        return await super().query(text)


def _use_client(factory: type[_LifecycleClaudeClient]):
    return patch.object(
        ClaudeRuntimeAdapter,
        "client",
        new=lambda _runtime, websocket, launch, on_progress, frames_to: factory(
            websocket, launch, on_progress, frames_to
        ),
    )


class _ClosingClaudeClient(_LifecycleClaudeClient):
    """Closes the session on connect, so the runner's loop exits at its first status check.

    Something has to end the loop, which otherwise sits in a 30s `wait_for_prompt`; ending it from
    the client keeps the store real and the loop's own exit condition under test.
    """

    on_connect: Callable[[], Awaitable[None]] | None = None

    async def connect(self) -> dict[str, Any]:
        response = await super().connect()
        on_connect = type(self).on_connect
        assert on_connect is not None
        await on_connect()
        return response


async def _allocated_session(chat_service: SessionService, recording_claims: RecordingClaims, operator_id: UUID):
    """Seed a claim-backed session for tests whose subject starts after allocation."""
    view, token = await chat_service._store._create_provisioning_for_test(operator_id)
    await recording_claims.create(
        session_id=view.session_id, bridge_token=token, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    return view


async def test_session_lifecycle_creates_claim_accepts_bridge_and_disposes_claim(
    allocator, session_store, recording_claims, session_wakes, operator_id
) -> None:
    websocket = _LifecycleWebSocket()
    chat_service = SessionService(
        configured_runtimes(recording_claims, client_factory=_ClosingClaudeClient), session_store, session_wakes
    )

    session = await chat_service.create(operator_id)
    session_id = session.session_id
    assert recording_claims.created == [], "an empty session owns no sandbox"
    await chat_service.enqueue_prompt(operator_id, session_id, "start", SPA_ORIGIN)
    await allocator.allocate_once()
    _ClosingClaudeClient.on_connect = lambda: session_store.request_close(operator_id, session_id)
    await chat_service.handle_runner(cast(Any, websocket), session_id, recording_claims.tokens[session_id])

    assert recording_claims.created == [session_id]
    assert websocket.accepted is True
    assert websocket.closed is None
    assert recording_claims.deleted == [session_id]
    assert await session_store.status(session_id) == SessionStatus.CLOSED
    # Cleanup is recorded by stamping `claim_cleaned_at`, which is what takes the session back out
    # of the reconciler's candidate set.
    assert await session_store.claim_cleanup_candidates() == []
    # The session bearer arrives in the pod through the SandboxClaim environment and is the Agent's
    # own authority, so Claude may use it through native MCP support or an ordinary HTTP client. The
    # provider API credential remains the separate non-secret egress-proxy placeholder.
    launch = cast(Any, _ClosingClaudeClient.last_launch)
    assert json.loads(launch.arguments[launch.arguments.index("--mcp-config") + 1]) == {
        "mcpServers": {
            "haku-console": {
                "type": "http",
                "url": "http://haku-console.test:9090/mcp",
                "headers": {"Authorization": "Bearer ${HAKU_AGENT_SDK_RUNNER_TOKEN}"},
            }
        }
    }
    assert "--strict-mcp-config" in launch.arguments
    assert launch.environment["CLAUDE_CODE_OAUTH_TOKEN"] == "not-a-secret"
    # The bearer is injected into the pod rather than copied into Console-selected environment
    # overrides. `test_runner` pins that it is inherited by the Agent process.
    assert recording_claims.tokens[session_id] not in launch.environment.values()


async def test_a_launch_that_cannot_be_built_fails_before_accepting_and_releases_the_claim(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    websocket = _LifecycleWebSocket()

    with patch.object(ClaudeRuntimeAdapter, "build_launch", side_effect=ValueError("invalid MCP endpoint")):
        await chat_service.handle_runner(
            cast(Any, websocket), session.session_id, recording_claims.tokens[session.session_id]
        )

    assert websocket.accepted is False
    assert websocket.closed == (1011, "runtime launch preparation failed")
    assert await session_store.status(session.session_id) == SessionStatus.FAILED
    assert recording_claims.deleted == [session.session_id]


async def test_the_first_idle_prompt_creates_the_claim_once(
    allocator, session_store, chat_service, recording_claims, operator_id
) -> None:
    session = await chat_service.create(operator_id)
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
    session = await chat_service.create(operator_id)

    view = await chat_service.sandbox_provisioning(operator_id, session.session_id)

    assert view.status == SessionStatus.IDLE
    assert view.sandbox is None
    assert recording_claims.inspected == []


class _RollingClaudeClient(_LifecycleClaudeClient):
    """Stands in for this replica being cancelled mid-session, which is what a roll is."""

    async def connect(self) -> dict[str, Any]:
        await super().connect()
        raise asyncio.CancelledError


async def test_a_rolling_replica_hands_the_session_back_instead_of_ending_it(
    session_store, recording_claims, session_wakes, operator_id
) -> None:
    """A roll cancels `handle_runner`. Failing the row there refuses the runner's reconnect as
    terminal and replaces the whole session, which at six rolls a day is the ordinary end of a
    conversation."""
    websocket = _LifecycleWebSocket()
    chat_service = SessionService(
        configured_runtimes(recording_claims, client_factory=_RollingClaudeClient), session_store, session_wakes
    )

    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    with pytest.raises(asyncio.CancelledError):
        await chat_service.handle_runner(cast(Any, websocket), session_id, recording_claims.tokens[session_id])

    assert await session_store.status(session_id) == SessionStatus.READY, "a roll is not a session ending"
    assert recording_claims.deleted == [], "the sandbox outlives the replica that was serving it"
    assert websocket.closed == (GOING_AWAY_CODE, "console replica going away"), (
        "the runner reconnects because it was told to, not because it guessed"
    )


async def test_a_returning_runner_is_admitted_and_takes_the_lease(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """A runner whose replica went away is admitted by the next one, which keeps the sandbox."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    token = recording_claims.tokens[session_id]
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED

    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
        assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.HELD, (
            "a replica still renewing its lease keeps the session it is serving — but only until it lapses"
        )
        await session_store.release_lease(session_id)
        assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED, (
            "a session handed back is adoptable by whichever replica the runner reaches"
        )


async def _folded(session_store: SessionStore, session_id: UUID, turn_id: UUID, *payloads: dict[str, Any]) -> None:
    """Record these frames and apply what they mean, the way the turn loop does — what a departed
    holder leaves behind, written through the same path rather than into the rows directly. The fold
    is threaded across them for the same reason the loop threads it: a message spans frames."""
    handler = ClaudeRuntimeAdapter().turn_handler()
    for payload in payloads:
        recorded = await session_store.record_frame(
            session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, payload
        )
        effects = handler.apply(frame_seq=recorded.frame_seq, frame=HarnessFrame(frame=payload))
        if effects.checkpoint is Checkpoint.ADVANCE:
            await session_store.apply_frame(session_id, turn_id, recorded.frame_seq, effects.events)
        else:
            assert not effects.events


async def test_adoption_picks_the_answer_up_where_it_stopped(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """The runner replays what a console may not have recorded but never the deltas, so a resumed
    turn starting from an empty string would write the tail of the answer over the head of it.
    Adoption says which turn, and hands back the message it was being written into.
    """
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    await session_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await session_store.enqueue_prompt(operator_id, session_id, "what were we doing", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    await _folded(session_store, session_id, started.turn_id, text_delta("we were half way through"))

    resumed = await session_store.adopt_open_turn(session_id)

    assert resumed is not None
    assert resumed.streaming is not None
    assert resumed.streaming.text == "we were half way through"
    state = await session_store.turn_state(resumed.turn_id)
    assert not state.said_anything, "the message is still open, so nothing has completed"


async def test_adoption_deduplicates_a_completed_copy_of_a_streamed_tool_call(
    session_store, chat_service, migrated_sessions, recording_claims, operator_id
) -> None:
    """The stream declaration can commit before a roll and its completed copy arrive afterwards."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    await session_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await session_store.enqueue_prompt(operator_id, session_id, "continue", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    await _folded(
        session_store,
        session_id,
        started.turn_id,
        tool_use_start("toolu_01", "Bash", index=1),
        input_json_delta('{"command": "true"}', index=1),
        content_block_stop(index=1),
    )

    resumed = await session_store.adopt_open_turn(session_id)
    assert resumed is not None
    assert resumed.seen_call_ids == frozenset({"toolu_01"})

    client = _FakeCli(
        [
            assistant(tool_use_block("toolu_01", "Bash", {"command": "true"})),
            tool_result("toolu_01", "ok", structured={"stdout": "ok"}),
            result(text="continued"),
        ]
    )
    client.replay()
    async with asyncio.timeout(30):
        await chat_service._run_turn(
            cast(Any, client), client.frames().__aiter__(), session_id, resumed, abort_event=asyncio.Event()
        )

    calls = [
        item for item in await session_items(migrated_sessions, session_id) if item.item_type is ItemType.TOOL_CALL
    ]
    assert len(calls) == 1
    assert (calls[0].call_id, calls[0].item_text, calls[0].status) == ("toolu_01", "ok", ItemStatus.COMPLETE)


async def test_adoption_restores_open_reasoning_and_completed_call_ids_for_provider_owned_state(
    session_store, chat_service, migrated_sessions, recording_claims, operator_id
) -> None:
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    await session_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await session_store.enqueue_prompt(operator_id, session_id, "continue", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    reasoning = await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"native": "reasoning"}
    )
    await session_store.apply_frame(
        session_id,
        started.turn_id,
        reasoning.frame_seq,
        (
            ReasoningStarted(provenance=FrameRange(reasoning.frame_seq, reasoning.frame_seq)),
            ItemSegment(
                item=OpenRef(ItemType.REASONING),
                text="half thought",
                provenance=FrameRange(reasoning.frame_seq, reasoning.frame_seq),
            ),
        ),
    )
    call = await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"native": "completed-call"}
    )
    await session_store.apply_frame(
        session_id,
        started.turn_id,
        call.frame_seq,
        (
            ToolCallStarted(
                call_id="call-done",
                tool_name="commandExecution",
                arguments={"command": "true", "cwd": "/workspace"},
                provenance=FrameRange(call.frame_seq, call.frame_seq),
            ),
            ToolCallCompleted(
                item=CallRef("call-done"),
                structured={"exitCode": 0},
                outcome=ToolOutcome.SUCCEEDED,
                provenance=FrameRange(call.frame_seq, call.frame_seq),
            ),
        ),
    )

    resumed = await session_store.adopt_open_turn(session_id)

    assert resumed is not None
    assert resumed.reasoning is not None
    assert resumed.reasoning.text == "half thought"
    assert resumed.seen_call_ids == frozenset({"call-done"})
    assert resumed.completed_call_ids == frozenset({"call-done"})
    assert _inherited(resumed) == TurnProjectionSeed(
        open_reasoning=OpenItemSeed(
            text="half thought",
            first_frame_seq=resumed.reasoning.first_frame_seq,
            last_frame_seq=resumed.reasoning.last_frame_seq,
        ),
        seen_call_ids=frozenset({"call-done"}),
        completed_call_ids=frozenset({"call-done"}),
    )


async def test_adoption_replays_a_tool_call_composition_from_its_start(
    session_store, chat_service, migrated_sessions, recording_claims, operator_id
) -> None:
    """No durable item can hold half a JSON value, so a roll replays the composition whole."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    await session_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await session_store.enqueue_prompt(operator_id, session_id, "continue", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    await _folded(
        session_store,
        session_id,
        started.turn_id,
        tool_use_start("toolu_01", "Bash", index=1),
        input_json_delta('{"command": ', index=1),
    )

    resumed = await session_store.adopt_open_turn(session_id)
    assert resumed is not None
    assert [frame.envelope.frame["type"] for frame in resumed.replay] == ["user", "stream_event", "stream_event"]

    client = _FakeCli(
        [
            input_json_delta('"true"}', index=1),
            content_block_stop(index=1),
            tool_result("toolu_01", "ok", structured={"stdout": "ok"}),
            result(text="continued"),
        ]
    )
    client.replay()
    async with asyncio.timeout(30):
        await chat_service._run_turn(
            cast(Any, client),
            _replaying(resumed.replay, client.frames().__aiter__()),
            session_id,
            resumed,
            abort_event=asyncio.Event(),
        )

    call = one(
        item for item in await session_items(migrated_sessions, session_id) if item.item_type is ItemType.TOOL_CALL
    )
    assert (call.arguments, call.item_text, call.status) == ({"command": "true"}, "ok", ItemStatus.COMPLETE)


async def test_a_turn_that_said_something_the_room_could_not_hear_still_knows_it_spoke(
    session_store, chat_service, migrated_sessions, operator_id
) -> None:
    """The resumed turn has to read that a message already completed, or `result.result` — which
    repeats it — becomes a message of its own.
    """
    view, token = await session_store.create(operator_id)
    session_id = view.session_id
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, session_id, "why did it fail?", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    await _folded(
        session_store,
        session_id,
        started.turn_id,
        assistant(text_block("a bad config"), message_id="msg_1"),
        # A frame of another message with no prose in it: what closes the one above, the way the
        # wire closes a message, without opening a second one to compare against.
        assistant(thinking_block("checking"), message_id="msg_2"),
    )
    said = one(
        item
        for item in await session_items(migrated_sessions, session_id)
        if item.item_type is ItemType.MESSAGE and item.status is ItemStatus.COMPLETE
    )

    resumed = await session_store.adopt_open_turn(session_id)
    assert resumed is not None
    assert (await session_store.turn_state(resumed.turn_id)).said_anything

    client = _FakeCli([result(text="a bad config")])
    client.replay()
    async with asyncio.timeout(30):
        await chat_service._run_turn(
            cast(Any, client), client.frames().__aiter__(), session_id, resumed, abort_event=asyncio.Event()
        )

    spoken = [
        item
        for item in await session_items(migrated_sessions, session_id)
        if item.item_type is ItemType.MESSAGE and item.item_text
    ]
    assert [item.item_id for item in spoken] == [said.item_id], "the result frame repeated a message, not made one"


async def test_adoption_closes_a_turn_whose_result_nobody_projected(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """The exchange is over and its `result` sits past the session's cursor, so nothing acted on it.

    Waiting for it on the socket would wait forever — the runner replays that frame and
    `record_frame` refuses it as one this session already has — so adoption hands it back as a
    frame to project, and projecting it closes the turn through the loop a live frame goes through.
    """
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    await session_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await session_store.enqueue_prompt(operator_id, session_id, "what were we doing", SPA_ORIGIN)
    assert await session_store.next_prompt(session_id) is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, result(uuid="res-1")
    )

    resumed = await session_store.adopt_open_turn(session_id)
    assert resumed is not None
    assert [frame.envelope.frame["type"] for frame in resumed.replay] == ["user", "result"]
    client = _FakeCli()
    async with asyncio.timeout(30):
        await chat_service._run_turn(
            cast(Any, client),
            _replaying(resumed.replay, client.frames().__aiter__()),
            session_id,
            resumed,
            abort_event=asyncio.Event(),
        )

    [turn] = await session_store.list_turns(str(session_id), cursor=None, limit=5, scope=UnrestrictedReads())
    assert turn.end == TurnAnsweredEnd()


async def test_adoption_reads_a_failed_result_as_a_failed_turn(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """`is_error` is `false` on every production result, including all 27 sessions the console
    recorded as failed, so closing from it adopts a turn that
    ended badly as answered. The projection of the frame closes the turn instead, so recovery fails
    exactly as the live path fails on the same frame.
    """
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    await session_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await session_store.enqueue_prompt(operator_id, session_id, "what were we doing", SPA_ORIGIN)
    assert await session_store.next_prompt(session_id) is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    await session_store.record_frame(
        session_id,
        FrameDirection.FROM_AGENT,
        BridgeFrameKind.HARNESS_FRAME,
        result(uuid="res-1", subtype="error_during_execution"),
    )

    resumed = await session_store.adopt_open_turn(session_id)
    assert resumed is not None
    client = _FakeCli()
    async with asyncio.timeout(30):
        await chat_service._run_turn(
            cast(Any, client),
            _replaying(resumed.replay, client.frames().__aiter__()),
            session_id,
            resumed,
            abort_event=asyncio.Event(),
        )

    [turn] = await session_store.list_turns(str(session_id), cursor=None, limit=5, scope=UnrestrictedReads())
    assert isinstance(turn.end, TurnFailedEnd)
    # Claude states nothing about the session on a failed result, and its CLI answers the next
    # prompt like any other, so the exchange failing leaves the session usable.
    assert await session_store.status(session_id) != SessionStatus.FAILED


async def test_a_turn_whose_cursor_is_behind_it_is_failed_rather_than_resumed(
    session_store, chat_service, recording_claims, migrated_sessions, operator_id
) -> None:
    """A cursor from before the turn names a position this turn's writes never took, so resuming
    from it would redo effects that did commit — a duplicated message and a duplicated room reply.

    `next_prompt` anchors the cursor at the frame before the turn, so no session that can still
    acquire a frame is in this state; one that somehow is has its turn ended rather than resumed.

    The turn has to open past frame 1 for the state to be expressible at all: the anchor is
    `first_frame_seq - 1`, so a turn opening at 1 anchors at 0 — which is also "nothing has ever
    projected" — and there is no position below it to put the cursor.
    """
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    await session_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "system"}
    )
    await session_store.enqueue_prompt(operator_id, session_id, "what were we doing", SPA_ORIGIN)
    assert await session_store.next_prompt(session_id) is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    async with migrated_sessions() as db:
        await db.execute(update(Session).where(Session.session_id == session_id).values(projected_frame_seq=0))
        await db.commit()

    assert await session_store.adopt_open_turn(session_id) is None

    [turn] = await session_store.list_turns(str(session_id), cursor=None, limit=5, scope=UnrestrictedReads())
    assert isinstance(turn.end, TurnFailedEnd)


async def test_a_turn_that_never_asked_its_prompt_gives_it_back(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """`next_prompt` claims the prompt; `_run_turn` writes it afterwards. A replica dying between
    the two asked nothing, so the prompt is owed a second offer rather than a silent burial.
    """
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    await session_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await session_store.enqueue_prompt(operator_id, session_id, "what were we doing", SPA_ORIGIN)
    claimed = await session_store.next_prompt(session_id)
    assert claimed is not None

    assert await session_store.adopt_open_turn(session_id) is None, "nothing to resume; nothing was asked"

    reoffered = await session_store.next_prompt(session_id)
    assert reoffered is not None, "a prompt that never left is still waiting to be asked"
    assert reoffered.item_id == claimed.item_id
    assert reoffered.prompt == "what were we doing"
    assert reoffered.turn_id != claimed.turn_id


async def test_a_turn_that_asked_its_prompt_keeps_it(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """The agent has it and the runner will replay its answer, so re-offering would ask twice."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    await session_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await session_store.enqueue_prompt(operator_id, session_id, "what were we doing", SPA_ORIGIN)
    assert await session_store.next_prompt(session_id) is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )

    assert await session_store.adopt_open_turn(session_id) is not None

    assert await session_store.next_prompt(session_id) is None, "the queue has nothing; the turn has it"


async def test_an_incapable_rolling_replica_retries_before_taking_the_lease(
    session_store, recording_claims, session_wakes, operator_id
) -> None:
    """A new runtime row can reach an old replica while its replacement rolls out.

    Capability is checked before bridge authentication because authentication is also lease
    acquisition. The old replica must not mark the session Ready or make every capable replica
    wait for its lease to expire; a 503 leaves the first attachment untouched and the runner's
    existing redial loop finds a replica with the execution resources.
    """
    capable = SessionService(configured_runtimes(recording_claims), session_store, session_wakes)
    session = await _allocated_session(capable, recording_claims, operator_id)
    token = recording_claims.tokens[session.session_id]
    incapable = SessionService(
        RuntimeRegistry({RuntimeKind.CLAUDE_CODE: ClaudeRuntimeAdapter()}), session_store, session_wakes
    )
    websocket = _LifecycleWebSocket()

    await incapable.handle_runner(cast(Any, websocket), session.session_id, token)

    assert websocket.denied == 503
    assert websocket.closed is None
    assert not websocket.accepted
    assert await session_store.status(session.session_id) == SessionStatus.PROVISIONING
    assert await session_store.authenticate_bridge(session.session_id, token) == BridgeAuthentication.ACCEPTED


async def test_a_held_session_tells_the_runner_to_retry_rather_than_refusing_it(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """A runner redials about a second after its socket drops, so it routinely reaches a new replica
    while the dying one's lease is still valid. Closing before `accept()` reaches it as 403 whatever
    code is passed, and 403 is a refusal it correctly gives up on — costing the sandbox. 503 is what
    it waits out."""
    session = await _allocated_session(chat_service, recording_claims, operator_id)
    session_id = session.session_id
    token = recording_claims.tokens[session_id]
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    websocket = _LifecycleWebSocket()

    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
        await chat_service.handle_runner(cast(Any, websocket), session_id, token)

    assert websocket.denied == 503
    assert websocket.closed is None, "a close before accept is the 403 this exists to avoid"
    assert not websocket.accepted


async def test_a_bad_credential_is_still_refused_outright(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """The other side of the distinction: a runner that will never be admitted must not spend its
    redial budget finding that out."""
    session = await chat_service.create(operator_id)
    websocket = _LifecycleWebSocket()

    await chat_service.handle_runner(cast(Any, websocket), session.session_id, "wrong")

    assert websocket.denied is None
    assert websocket.closed == (NOT_ADMITTED_CODE, "invalid or consumed runner credential")


async def test_terminal_runner_retry_deletes_its_stale_claim(
    session_store, chat_service, recording_claims, operator_id
) -> None:
    """A runner presenting a valid credential for an already-closed session is turned away."""
    websocket = _LifecycleWebSocket()

    session = await _allocated_session(chat_service, recording_claims, operator_id)
    await session_store.request_close(operator_id, session.session_id)

    await chat_service.handle_runner(
        cast(Any, websocket), session.session_id, recording_claims.tokens[session.session_id]
    )

    assert recording_claims.deleted == [session.session_id]
    assert await session_store.claim_cleanup_candidates() == []
    assert await session_store.status(session.session_id) == SessionStatus.CLOSED
    assert websocket.closed == (1008, "runner session is already terminal")


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

_NARRATED_TURN = [
    assistant(text_block("Looking at the logs now.")),
    assistant(tool_use_block("toolu_01", "Bash", {"command": "true"})),
    assistant(text_block("Found it: a bad config.")),
    result(text="Found it: a bad config."),
]


class _InterruptedCli(_FakeCli):
    """Aborts once its script has run out, and answers `interrupt` with a `result` frame, as a real
    CLI does and as the turn loop drains to.

    **Where the abort lands is the point.** A real one arrives between frames, with the turn parked
    on `anext`, so this fires it exactly there: when the loop asks for a frame that has not been
    sent. One that lands while a frame is already in hand does not exercise the drain at all.
    """

    def __init__(self, script: list[dict[str, Any]], *, abort_event: asyncio.Event):
        super().__init__(script)
        self._abort_event = abort_event

    async def interrupt(self) -> None:
        await super().interrupt()
        self.deliver(result(text="stopped"))

    async def frames(self):
        source = super().frames()
        for _ in self.script:
            yield await anext(source)
        self._abort_event.set()
        async for frame in source:
            yield frame


class _CliFinishingItsMessage(_InterruptedCli):
    """Interrupted mid-message, and finishes that message before the `result`, as a real CLI does
    when the interrupt reaches it with a message already part written.

    `_InterruptedCli` on its own cannot reach that: the `result` is the first thing its drain sees.
    """

    def __init__(self, script: list[dict[str, Any]], *, abort_event: asyncio.Event, finishing: dict[str, Any]) -> None:
        super().__init__(script, abort_event=abort_event)
        self._finishing = finishing

    async def interrupt(self) -> None:
        # Queued before `super()` queues the `result`, so the message the CLI was writing arrives
        # ahead of the frame that ends the turn — which is the order that makes it a drained one.
        self.deliver(self._finishing)
        await super().interrupt()


async def _turn_into_a_room(
    session_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    recording_claims: RecordingClaims,
    session_wakes: SessionWakes,
    operator_id: UUID,
    client: _FakeCli,
    *,
    abort_event: asyncio.Event | None = None,
) -> list[str]:
    """Run one turn against *client* for a room-backed session and return what the room is owed."""
    service = SessionService(configured_runtimes(recording_claims), session_store, session_wakes)
    view, token = await session_store.create(operator_id)
    assert token is not None
    await attach_channel(migrated_sessions, view.session_id, ROOM)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    async with asyncio.timeout(30):
        await service._run_turn(
            client, client.frames().__aiter__(), view.session_id, turn, abort_event=abort_event or asyncio.Event()
        )
    return await answers(migrated_sessions, view.session_id)


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
    spa, _ = await session_store.create(operator_id)
    attached, _ = await session_store.create(operator_id)
    await attach_channel(migrated_sessions, attached.session_id, ROOM)

    assert await service._appended_prompt(spa.session_id) is None
    assert await service._appended_prompt(attached.session_id) == f"{attached.session_id} 0"


async def test_a_resumed_turn_finishes_the_answer_it_inherited(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """The replacement replica finishes the exchange the dead one started, in the message it
    started, and the room is owed the answer once."""
    service = SessionService(configured_runtimes(recording_claims), session_store, session_wakes)
    view, token = await session_store.create(operator_id)
    session_id = view.session_id
    await attach_channel(migrated_sessions, session_id, ROOM)
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, session_id, "why did it fail?", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    # What the previous holder got through before its pod went: the prompt written, and half an
    # answer streamed into a message it never closed — applied as the loop applies any frame, so
    # the message row, the turn's pointer at it and the session's cursor all landed together.
    delta = text_delta("because the ")
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    opened_at = await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, delta
    )
    effects = (
        ClaudeRuntimeAdapter().turn_handler().apply(frame_seq=opened_at.frame_seq, frame=HarnessFrame(frame=delta))
    )
    await session_store.apply_frame(session_id, started.turn_id, opened_at.frame_seq, effects.events)
    half_answered = one(await _open_items(migrated_sessions, session_id))

    resumed = await session_store.adopt_open_turn(session_id)
    assert resumed is not None
    assert resumed.replay == (), "the cursor passed every recorded frame, so none of them is redone"
    # Only what the runner replays: the deltas already seen are not re-sent, so everything
    # before "disk was full" reaches this process solely through the turn's own row.
    client = _FakeCli([assistant(text_block("because the disk was full")), result(text="done")])
    client.replay()
    async with asyncio.timeout(30):
        await service._run_turn(
            client,
            _replaying(resumed.replay, client.frames().__aiter__()),
            session_id,
            resumed,
            abort_event=asyncio.Event(),
        )

    assert await answers(migrated_sessions, session_id) == ["because the disk was full"], "not the answer twice"
    said = [item for item in await session_items(migrated_sessions, session_id) if item.item_type is ItemType.MESSAGE]
    assert [item.item_id for item in said] == [half_answered], "continued, rather than forked into a second"
    [turn] = await session_store.list_turns(str(session_id), cursor=None, limit=5, scope=UnrestrictedReads())
    assert (turn.turn_id, turn.end) == (started.turn_id, TurnAnsweredEnd())


async def test_adoption_redoes_the_frames_past_the_cursor_and_only_those(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """Two frames are in the log and the departed holder projected one of them.

    The cursor is the whole of what tells them apart, and both errors it prevents are visible from
    the room: redoing the projected delta would append its prose to the message a second time,
    while not redoing the unprojected answer would lose it outright — the runner will not offer a
    frame this session already recorded, so nothing else is coming to write it down.
    """
    service = SessionService(configured_runtimes(recording_claims), session_store, session_wakes)
    view, token = await session_store.create(operator_id)
    session_id = view.session_id
    await attach_channel(migrated_sessions, session_id, ROOM)
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, session_id, "why did it fail?", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    delta = text_delta("because the ")
    answer = assistant(text_block("because the disk was full"))
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    recorded = await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, delta
    )
    effects = ClaudeRuntimeAdapter().turn_handler().apply(frame_seq=recorded.frame_seq, frame=HarnessFrame(frame=delta))
    await session_store.apply_frame(session_id, started.turn_id, recorded.frame_seq, effects.events)
    # Recorded and then nothing: the pod went between the sink writing the row and the loop acting
    # on what it meant.
    unprojected = await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, answer
    )

    resumed = await session_store.adopt_open_turn(session_id)
    assert resumed is not None
    assert [frame.frame_seq for frame in resumed.replay] == [unprojected.frame_seq]
    client = _FakeCli([result(text="because the disk was full")])
    client.replay()
    async with asyncio.timeout(30):
        await service._run_turn(
            client,
            _replaying(resumed.replay, client.frames().__aiter__()),
            session_id,
            resumed,
            abort_event=asyncio.Event(),
        )

    assert await answers(migrated_sessions, session_id) == ["because the disk was full"]


async def test_the_room_is_owed_each_assistant_message_as_it_finishes(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """A turn that says what it is about to do, works, then reports back is three messages in the
    transcript, and the room gets all three rather than only the conclusion."""
    queued = await _turn_into_a_room(
        session_store, migrated_sessions, recording_claims, session_wakes, operator_id, _FakeCli(_NARRATED_TURN)
    )

    assert queued == ["Looking at the logs now.", "Found it: a bad config."]


async def test_the_last_message_is_not_repeated_by_the_result_frame(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """`result.result` carries the same text as the turn's last assistant message, so queueing
    both would post the answer twice."""
    queued = await _turn_into_a_room(
        session_store, migrated_sessions, recording_claims, session_wakes, operator_id, _FakeCli(_NARRATED_TURN)
    )

    assert queued.count("Found it: a bad config.") == 1


async def test_the_room_is_owed_the_answer_before_the_turn_can_fail(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """The drop that needs neither a reconnection nor a roll: a turn that failed after producing
    text. The ending frame's own events are never applied, so the
    message the turn was mid-way through is closed by `close_answer` or by nothing at all — and a
    message left open is prose no channel is owed.
    """
    service = SessionService(configured_runtimes(recording_claims), session_store, session_wakes)
    view, token = await session_store.create(operator_id)
    await attach_channel(migrated_sessions, view.session_id, ROOM)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    client = _FakeCli([*_NARRATED_TURN[:-1], result(subtype="error_during_execution", is_error=True)])

    async with asyncio.timeout(30):
        await service._run_turn(client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event())

    assert await answers(migrated_sessions, view.session_id) == ["Looking at the logs now.", "Found it: a bad config."]


async def test_a_turn_the_cli_ended_badly_fails_even_though_is_error_says_it_did_not(
    session_store, chat_service, operator_id
) -> None:
    """`is_error` is false on all 129 production `result` frames — including every one of the 27
    sessions the console recorded as failed — so a loop reading it calls every turn fine. The turn's
    outcome is the projection's, and that reads `subtype` (<claude_code/projection.py>).
    """
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "keep going", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    client = _FakeCli([result(subtype="error_max_turns")])

    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event()
    )

    [record] = await session_store.list_turns(view.session_id, cursor=None, limit=5, scope=UnrestrictedReads())
    assert record.end == TurnFailedEnd(failure="error_max_turns: end_turn")
    assert await session_store.status(view.session_id) != SessionStatus.FAILED


async def test_a_turn_whose_answer_arrived_only_on_the_result_is_still_spoken(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """No assistant message completed, so nothing was said along the way — the `result` frame is
    the only thing that keeps the room from hearing silence."""
    queued = await _turn_into_a_room(
        session_store,
        migrated_sessions,
        recording_claims,
        session_wakes,
        operator_id,
        _FakeCli([result(text="nothing streamed, but an answer")]),
    )

    assert queued == ["nothing streamed, but an answer"]


async def test_a_turn_with_nothing_at_all_to_say_records_no_empty_answer(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """There is no silence token, and an empty answer is not one.

    The durable turn end lets each channel derive its own silence notice; the turn loop only leaves
    no completed message for it to deliver.
    """

    queued = await _turn_into_a_room(
        session_store, migrated_sessions, recording_claims, session_wakes, operator_id, _FakeCli([result(text="")])
    )

    assert queued == []


async def test_an_aborted_turn_leaves_a_notice_and_no_reply(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """Two things. The operator's stop reaches the room as a notice and nothing else — no
    `session_outbox` row, because the fact is a `session_events` row and the notice is its
    projection. And the turn has to *survive* the abort: draining to the interrupt's `result` must
    not open a second `anext` on the session's generator, which an async generator refuses, since
    an abort lands exactly there — between frames.
    """
    abort_event = asyncio.Event()
    client = _InterruptedCli(_NARRATED_TURN[:-1], abort_event=abort_event)

    queued = await _turn_into_a_room(
        session_store, migrated_sessions, recording_claims, session_wakes, operator_id, client, abort_event=abort_event
    )

    assert client.interrupted
    # The two messages, and nothing from the interrupt's own `result` frame ("stopped"). That the
    # stop itself is recorded is `test_session_store`'s; the room reads that row for itself
    # (<channels/matrix/conversation_subscriber.py>) rather than being told here.
    assert queued == ["Looking at the logs now.", "Found it: a bad config."]


async def test_an_abort_mid_answer_leaves_the_half_answer_unmarked(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """Stopped between deltas, so no assistant message ever completed: the message row that closes
    the stream carries the half answer and only that. The stop is the console's fact, not part of
    what the agent said, so it does not get written into the agent's words.
    """
    abort_event = asyncio.Event()
    client = _InterruptedCli([text_delta("because the "), text_delta("disk was full")], abort_event=abort_event)

    queued = await _turn_into_a_room(
        session_store, migrated_sessions, recording_claims, session_wakes, operator_id, client, abort_event=abort_event
    )

    assert queued == ["because the disk was full"]


async def test_a_message_the_agent_finished_before_stopping_survives_the_drain(
    session_store, migrated_sessions, recording_claims, session_wakes, operator_id
) -> None:
    """A reply can remain open when the runtime raises before its delivery row is created.

    An abort does not land between messages; it lands inside one, and the CLI finishes what it was
    writing before it stops. Draining only to the `result` discards that `assistant` frame
    entirely, leaving the text in `session_frames` where no operator is looking. It is a message
    like any other, so the room is owed it like any other.
    """
    abort_event = asyncio.Event()
    client = _CliFinishingItsMessage(
        [assistant(text_block("Looking at the logs now."))],
        abort_event=abort_event,
        finishing=assistant(text_block("Found it: a bad config.")),
    )

    queued = await _turn_into_a_room(
        session_store, migrated_sessions, recording_claims, session_wakes, operator_id, client, abort_event=abort_event
    )

    assert client.interrupted
    # The drained message once, and nothing from the interrupt's own `result` frame ("stopped"),
    # which the finished message is what makes redundant.
    assert queued == ["Looking at the logs now.", "Found it: a bad config."]


async def test_a_turn_brackets_the_frames_it_produced(session_store, chat_service, operator_id) -> None:
    """The bracket is what makes a turn's own frames findable afterwards."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    # A frame from before this turn, so a bracket that started at the log's beginning would show.
    await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "system"}
    )
    await session_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    answer = assistant(text_block("a bad config"))
    ending = result(text="a bad config")
    # In production the socket wrapper writes these, so the recorder and the turn loop see the same
    # frames; here the double is handed the numbers the log gave them.
    recorded_answer = await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, answer
    )
    recorded_ending = await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, ending
    )
    client = _FakeCli([answer, ending], frame_seqs=[recorded_answer.frame_seq, recorded_ending.frame_seq])

    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event()
    )

    [record] = await session_store.list_turns(view.session_id, cursor=None, limit=10, scope=UnrestrictedReads())
    assert record.end == TurnAnsweredEnd()
    assert (record.first_frame_seq, record.last_frame_seq) == (recorded_answer.frame_seq, recorded_ending.frame_seq)
    assert record.ended_at is not None


async def test_a_turn_ends_at_its_own_result_rather_than_at_what_the_cli_logs_after_it(
    session_store, chat_service, operator_id
) -> None:
    """The CLI emits a `command_lifecycle` frame just after the `result` one, so it is already in
    the log by the time the turn loop closes the turn, and a bound taken from the log's head reports
    it as the turn's last frame — on 80 of 99 production turns (2026-08-16)."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    ending = result(text="a bad config")
    recorded = await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, ending
    )
    await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "command_lifecycle"}
    )

    client = _FakeCli([ending], frame_seqs=[recorded.frame_seq])

    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event()
    )

    [record] = await session_store.list_turns(view.session_id, cursor=None, limit=10, scope=UnrestrictedReads())
    assert record.last_frame_seq == recorded.frame_seq


async def test_the_transcript_carries_what_each_tool_answered(
    session_store, chat_service, migrated_sessions, operator_id
) -> None:
    """The call and its answer are the same item, found by `call_id` — exact, where matching the Nth
    answer to the Nth call would be a guess, and needing no id from the agent: neither frame here
    carries a `message.id`, as 1,417 production assistant rows do not."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "count the files", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    client = _FakeCli(
        [
            assistant(tool_use_block("toolu_ok", "Bash", {"command": "true"})),
            assistant(tool_use_block("toolu_running", "Bash", {"command": "sleep 1"})),
            # As the CLI sends it: an answer is a `user` frame, and one call is left unanswered.
            tool_result("toolu_ok", "42"),
            result(text="done"),
        ]
    )
    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, abort_event=asyncio.Event()
    )

    calls = {
        item.call_id: item
        for item in await session_items(migrated_sessions, view.session_id)
        if item.item_type is ItemType.TOOL_CALL
    }

    answered = calls["toolu_ok"]
    # `UNKNOWN` because the frame carries no `is_error`, which is how the CLI sends a plain success
    # — an outcome the fold reports rather than guesses at.
    assert (answered.tool_name, answered.item_text, answered.outcome) == ("Bash", "42", ToolOutcome.UNKNOWN)
    running = calls["toolu_running"]
    assert (running.status, running.outcome) == (ItemStatus.OPEN, None), (
        "a call still running must not read as an empty answer"
    )


class _RealDbClaudeClient(_LifecycleClaudeClient):
    """Answers every prompt with "pong", then goes quiet like an idle CLI."""

    def __init__(self, adapter: object, launch: object, on_progress: object, frames_to: FrameSink):
        super().__init__(adapter, launch, on_progress, frames_to)
        self.script = [assistant(text_block("pong")), result(text="pong")]


async def test_runner_survives_an_idle_wait_against_a_real_database(
    session_store, chat_service, migrated_sessions, operator_id
) -> None:
    """The idle wait is a raw-driver call, so only a real engine exercises it.

    `handle_runner` loops: consume a prompt, then block in `wait_for_prompt` until the next one.
    That wait talks to `driver_connection` directly, so a driver-API mismatch there is invisible to
    any test that fakes the store — and it killed every Matrix session about four seconds in with
    "'Connection' object has no attribute 'set_autocommit'".
    """
    # The store mints the real bridge token; no claim is created because handle_runner only ever
    # deletes one on the way out, and Kubernetes is not what this test is about.
    view, token = await session_store.create(operator_id)

    with _use_client(_RealDbClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        try:
            # Long enough to reach the idle wait, which is where the crash used to happen.
            await asyncio.sleep(2)
            assert await session_store.status(view.session_id) == SessionStatus.READY, (
                "the runner failed while waiting for a prompt"
            )

            # And the wait must actually wake on NOTIFY rather than only time out. A bounded poll
            # rather than an Event, so the runner's wake is observed from outside; what it polls
            # for is the closed turn, since the session's status stays `ready` throughout.
            await session_store.enqueue_prompt(operator_id, view.session_id, "ping", SPA_ORIGIN)
            for _ in range(75):
                if [
                    turn
                    for turn in await session_store.list_turns(
                        str(view.session_id), cursor=None, limit=2, scope=UnrestrictedReads()
                    )
                    if turn.ended_at
                ]:
                    break
                await asyncio.sleep(0.2)
            [turn] = await session_store.list_turns(
                str(view.session_id), cursor=None, limit=2, scope=UnrestrictedReads()
            )
            assert turn.end == TurnAnsweredEnd(), "the turn never completed"
        finally:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    [answer] = [
        item for item in await session_items(migrated_sessions, view.session_id) if item.item_type is ItemType.MESSAGE
    ]
    assert answer.item_text == "pong"


class _ScriptedChannel:
    """A `FrameChannel` whose far end is a queue of the CLI's own frames."""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self._inbound: asyncio.Queue[HarnessFrame | None] = asyncio.Queue()
        self._wrote = asyncio.Event()

    def deliver(self, frame: dict[str, Any], *, seq: int | None = None) -> None:
        self._inbound.put_nowait(HarnessFrame(frame=frame, seq=seq))

    async def connect(self) -> None:
        pass

    async def write(self, frame: HarnessFrame) -> None:
        self.written.append(frame.frame)
        self._wrote.set()

    async def first_write(self) -> dict[str, Any]:
        """The opening frame, once it is actually on the wire.

        `claude_code.client._write` numbers a frame before writing it, and numbering here is a database
        round trip, so the write lands several loop turns after `connect()` is scheduled.
        """
        async with asyncio.timeout(30):
            await self._wrote.wait()
        return self.written[0]

    async def read_messages(self):
        while (message := await self._inbound.get()) is not None:
            yield message

    async def close(self) -> None:
        self._inbound.put_nowait(None)


async def _frames(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> list[SessionFrame]:
    async with sessions() as db:
        return list(
            await db.scalars(
                select(SessionFrame).where(SessionFrame.session_id == session_id).order_by(SessionFrame.frame_seq)
            )
        )


def _streamed(frames: Sequence[SessionFrame]) -> str:
    """The answer as the recorded deltas spell it, in log order."""
    return "".join(
        frame.payload["event"]["delta"]["text"]
        for frame in frames
        if frame.kind == BridgeFrameKind.HARNESS_FRAME and frame.payload.get("type") == DELTA_FRAME_KIND
    )


async def test_the_rollout_records_both_channels_both_ways_and_skips_only_deltas(
    session_store, migrated_sessions, operator_id
) -> None:
    """What the agent did is only recoverable from the wire.

    Tool results arrive as `user` frames, which the turn loop drops entirely, so the record is
    taken where every frame passes rather than from what the loop unpacks. **The control channel
    counts.** It never reaches `frames()`, so recording off the conversation queue would drop
    `interrupt` and its answer, and an interrupt that did not take is diagnosable from nothing else.
    """
    view, _ = await session_store.create(operator_id)
    answered = tool_result("toolu_1", "42")
    channel = _ScriptedChannel()
    cli = ClaudeCli(channel, RolloutRecorder(session_store, view.session_id), control_timeout=5)

    connecting = asyncio.create_task(cli.connect())
    initialize = await channel.first_write()
    channel.deliver(
        {"type": "control_response", "response": {"subtype": "success", "request_id": initialize["request_id"]}}
    )
    await connecting
    await cli.query("what did that return?")
    channel.deliver({"type": "stream_event", "event": {"type": "content_block_delta"}})
    channel.deliver(answered)
    # Reading is what proves the reader got that far; the recorder runs inside it.
    frames = cli.frames()
    delta_received = await anext(frames)
    assert delta_received.envelope.frame["type"] == "stream_event"
    result_received = await anext(frames)
    assert result_received.envelope.frame == answered
    await cli.aclose()

    # Every frame either way and no exceptions left — the delta included, which is what makes
    # this a log rather than a selection.
    recorded = await _frames(migrated_sessions, view.session_id)
    assert [(frame.direction, frame.kind) for frame in recorded] == [
        (FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME),
        (FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME),
        (FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME),
        (FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME),
        (FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME),
    ]
    assert [frame.payload["type"] for frame in recorded] == [
        "control_request",
        "control_response",
        "user",
        "stream_event",
        "user",
    ]
    # Verbatim inside the complete inner harness frame: a reader gets the tool result the turn loop never kept.
    assert recorded[4].payload == answered
    # Each frame reaches its consumer carrying the row it was written to, so a projection built
    # from it can point back at that row and not at whichever frame the reader has since seen.
    assert [delta_received.frame_seq, result_received.frame_seq] == [recorded[3].frame_seq, recorded[4].frame_seq]


async def test_the_runners_number_is_recorded_beside_the_rows_own(
    session_store, migrated_sessions, operator_id
) -> None:
    """Two numbers per row. `frame_seq` is Postgres's and is the log's ordering; `runner_seq` is
    the peer's and the only one a reconnect can hand back, which is what `highest_runner_seq`
    reads. A write to the CLI carries none: the runner numbers what it sends, not what it forwards.
    """
    view, _ = await session_store.create(operator_id)
    channel = _ScriptedChannel()
    cli = ClaudeCli(channel, RolloutRecorder(session_store, view.session_id), control_timeout=5)

    connecting = asyncio.create_task(cli.connect())
    initialize = await channel.first_write()
    channel.deliver(
        {"type": "control_response", "response": {"subtype": "success", "request_id": initialize["request_id"]}}, seq=11
    )
    await connecting
    channel.deliver(result(uuid="turn-1"), seq=12)
    assert (await anext(cli.frames())).envelope.frame["type"] == "result"
    await cli.aclose()

    recorded = await _frames(migrated_sessions, view.session_id)
    assert [(frame.kind, frame.runner_seq) for frame in recorded] == [
        (BridgeFrameKind.HARNESS_FRAME, None),
        (BridgeFrameKind.HARNESS_FRAME, 11),
        (BridgeFrameKind.HARNESS_FRAME, 12),
    ]
    assert [frame.payload["type"] for frame in recorded] == ["control_request", "control_response", "result"]
    assert await session_store.highest_runner_seq(view.session_id) == 12


class _DyingMidStreamClaudeClient(_LifecycleClaudeClient):
    """Streams two deltas, then ends the turn without ever completing the message."""

    def __init__(self, adapter: object, launch: object, on_progress: object, frames_to: FrameSink):
        super().__init__(adapter, launch, on_progress, frames_to)
        self.script = [text_delta("half an "), text_delta("answer"), result()]


class _DisconnectingClaudeClient(_LifecycleClaudeClient):
    """Exposes its instance so a test can drop the socket while the session sits idle."""

    instance: _DisconnectingClaudeClient | None = None

    def __init__(self, adapter: object, launch: object, on_progress: object, frames_to: FrameSink):
        super().__init__(adapter, launch, on_progress, frames_to)
        type(self).instance = self


async def test_an_idle_session_hands_back_the_instant_its_socket_drops(
    session_store, chat_service, migrated_sessions, operator_id
) -> None:
    """A roll drops the runner's socket while the session is between turns. It has to hand back
    then, rather than sit in the 30s prompt-wait until graceful shutdown cancels it: the connection
    watcher turns the drop into the disconnect the handler releases on. The proof is that the task
    ends on its own, with no cancel, and the session stays adoptable.
    """
    view, token = await session_store.create(operator_id)
    _DisconnectingClaudeClient.instance = None

    with _use_client(_DisconnectingClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        for _ in range(75):
            if (
                _DisconnectingClaudeClient.instance is not None
                and await session_store.status(view.session_id) == SessionStatus.READY
            ):
                break
            await asyncio.sleep(0.1)
        assert _DisconnectingClaudeClient.instance is not None
        assert await session_store.status(view.session_id) == SessionStatus.READY

        _DisconnectingClaudeClient.instance.disconnect()
        await asyncio.wait_for(runner, timeout=5)

    assert await session_store.status(view.session_id) in OPEN_SESSION_STATUSES, "handed back, not failed"
    holder, expires_at = await lease_of(migrated_sessions, view.session_id)
    assert holder is None
    assert expires_at <= datetime.now(UTC)


async def test_an_answer_cut_off_mid_stream_is_in_the_rollout(
    session_store, chat_service, migrated_sessions, operator_id
) -> None:
    """The deltas are the record, and each is written as it crosses the wire, so a turn no
    `assistant` frame ever completed still has its half-answer in the log. A finalizer could not
    reconstruct it: a replica losing its pod raises `CancelledError` straight past one.
    """
    view, token = await session_store.create(operator_id)

    with _use_client(_DyingMidStreamClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        try:
            for _ in range(75):
                if await session_store.status(view.session_id) == SessionStatus.READY:
                    break
                await asyncio.sleep(0.2)
            await session_store.enqueue_prompt(operator_id, view.session_id, "go", SPA_ORIGIN)
            # Waits for the whole streamed text, not for the first delta: waiting on one frame
            # existing races the second and cancels between them, asserting a timing rather than
            # the property.
            for _ in range(75):
                if _streamed(await _frames(migrated_sessions, view.session_id)) == "half an answer":
                    break
                await asyncio.sleep(0.2)
        finally:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    recorded = await _frames(migrated_sessions, view.session_id)
    assert _streamed(recorded) == "half an answer"
    assert not [frame for frame in recorded if frame.payload.get("type") == "assistant"], (
        "no frame completed the message"
    )


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

    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
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
    view, token = await session_store.create(operator_id)
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


async def test_a_cancelled_runner_hands_the_session_back_without_stranding_it(
    session_store, chat_service, migrated_sessions, operator_id
) -> None:
    """Pod termination cancels this task, and `CancelledError` is not an `Exception`, so neither
    `except` clause sees it. Neither answer at the two extremes works: leaving the row live strands
    a session nobody maintains, and failing it is terminal, which refuses the runner's reconnect and
    costs every roll its conversation. Handing it back keeps both properties — adoptable by
    whichever replica the runner reaches, and still caught by the sweep once the window passes.
    """
    view, token = await session_store.create(operator_id)

    with _use_client(_RealDbClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        await asyncio.sleep(2)  # Long enough to reach the idle wait, as the sibling test does.
        assert await session_store.status(view.session_id) == SessionStatus.READY

        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    assert await session_store.status(view.session_id) == SessionStatus.READY
    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
        assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)
    assert await session_store.expire_stale_leases() == 1
    assert await session_store.status(view.session_id) == SessionStatus.FAILED


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

    with patch("haku.console.x.session_runtime.OBSERVATION_TTL", timedelta(hours=1)):
        for _ in range(5):
            await chat_service.sandbox_provisioning(operator_id, session.session_id)
    assert recording_claims.inspected == [session.session_id]

    with patch("haku.console.x.session_runtime.OBSERVATION_TTL", timedelta(0)):
        await chat_service.sandbox_provisioning(operator_id, session.session_id)
    assert recording_claims.inspected == [session.session_id] * 2, (
        "a view past its budget is taken again rather than served stale"
    )


async def test_provisioning_is_not_readable_for_a_session_another_operator_owns(chat_service, operator_id) -> None:
    session = await chat_service.create(operator_id)

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
                    runtime_kind=RuntimeKind.CLAUDE_CODE,
                    created_at=datetime.now(UTC),
                )
            )
    assert not _transient_database_error(excinfo.value)


_WAKE_TURN_ONE = [
    assistant(text_block("Started the fetch in the background.")),
    result(text="Started the fetch in the background."),
]
# The idle-time shape pinned by `claude_code/testdata/background_wake.jsonl`: announcement chatter,
# then the exchange's own content with nothing on stdin.
_WAKE_CHATTER = [
    system("background_tasks_changed", tasks=[]),
    system("task_updated", task_id="task_1", patch={"status": "completed"}),
    system(
        "task_notification",
        task_id="task_1",
        status="completed",
        summary='Background command "fetch" completed (exit code 0)',
    ),
    system("init"),
    system("status", status="requesting"),
]
_WAKE_EXCHANGE = [
    text_delta("The fetch finished."),
    assistant(text_block("The fetch finished.")),
    result(text="The fetch finished."),
]


class _WakingClaudeClient(_LifecycleClaudeClient):
    """Answers the operator's turn from its script, then hands the test the wheel."""

    instance: object | None = None

    def __init__(self, adapter: object, launch: object, on_progress: object, frames_to: FrameSink):
        super().__init__(adapter, launch, on_progress, frames_to)
        self.script = list(_WAKE_TURN_ONE)
        type(self).instance = self

    async def arrive(self, frames_list: list[dict[str, Any]]) -> None:
        """Frames the CLI produces on its own: recorded through the real sink, then delivered."""
        for frame in frames_list:
            recorded_frame = await self._frames_to.received(HarnessFrame(frame=frame))
            self.deliver(frame, recorded_frame.frame_seq)


async def _eventually(read: Callable[[], Awaitable[bool]], *, saying: str) -> None:
    for _ in range(400):
        if await read():
            return
        await asyncio.sleep(0.025)
    pytest.fail(saying)


async def test_a_harness_initiated_exchange_becomes_an_ordinary_turn(
    allocator, session_store, recording_claims, session_wakes, operator_id, migrated_sessions
) -> None:
    """The session wakes itself — a background task's notification, then an unprompted exchange —
    and the console brackets it like any turn: a row, a harness-origin prompt item saying what
    woke it, the answer projected, and the cursor carried past it so the next operator prompt
    cannot swallow the wake's stale terminal frame."""
    websocket = _LifecycleWebSocket()
    runtimes = configured_runtimes(recording_claims, client_factory=_WakingClaudeClient)
    chat_service = SessionService(runtimes, session_store, session_wakes)
    session = await chat_service.create(operator_id)
    session_id = session.session_id
    await chat_service.enqueue_prompt(operator_id, session_id, "start the fetch", SPA_ORIGIN)
    await allocator.allocate_once()
    runner = asyncio.ensure_future(
        chat_service.handle_runner(cast(Any, websocket), session_id, recording_claims.tokens[session_id])
    )
    try:

        async def answered(expected: list[str]) -> bool:
            return await answers(migrated_sessions, session_id) == expected

        await _eventually(lambda: answered(["Started the fetch in the background."]), saying="turn one never completed")
        client = _WakingClaudeClient.instance
        assert isinstance(client, _WakingClaudeClient)
        await client.arrive(_WAKE_CHATTER + _WAKE_EXCHANGE)
        await _eventually(
            lambda: answered(["Started the fetch in the background.", "The fetch finished."]),
            saying="the wake exchange never completed",
        )
        # End the loop from the store, then nudge the idle wait with one more chatter frame so the
        # loop re-checks status without waiting out its notification timeout.
        await session_store.request_close(operator_id, session_id)
        await client.arrive([system("status", status="idle")])
        await asyncio.wait_for(runner, timeout=15)
    finally:
        runner.cancel()

    async with migrated_sessions() as db:
        turns = (
            await db.scalars(
                select(ConversationTurn)
                .where(ConversationTurn.session_id == session_id)
                .order_by(ConversationTurn.started_at)
            )
        ).all()
    assert [turn.outcome for turn in turns] == [TurnOutcome.ANSWERED, TurnOutcome.ANSWERED]
    entries = await session_store.read_item_rows(
        await session_store.conversation_of(session_id), after_seq=None, limit=100, scope=UnrestrictedReads()
    )
    prompts = [entry for entry in map(entry_of, entries) if isinstance(entry, PromptEntry)]
    assert [(prompt.text, prompt.origin) for prompt in prompts] == [
        ("start the fetch", PromptOriginKind.SPA),
        ('Background command "fetch" completed (exit code 0)', PromptOriginKind.HARNESS),
    ]
    assert client.prompts == ["start the fetch"], "a wake turn asks no question"
    async with migrated_sessions() as db:
        report = await reprojection.check_session(db, session_id, runtimes=runtimes)
    assert [turn.outcome for turn in report.turns] == [reprojection.Agrees(), reprojection.Agrees()]


if __name__ == "__main__":
    pytest_bazel.main()
