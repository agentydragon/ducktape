"""Focused contracts for the Agent Sandbox Claude chat runtime.

**No channel is imported here, deliberately.** A room reaches this file only as the `ChatFrontend`
port `session_runtime.py` defines and a `chat_attachment` address — never as `matrix-nio`, ingress
or the room/session binding. What a homeserver's messages become is
<channels/matrix/test_conversation.py>, beside the `MatrixTurns` that makes them turns.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import (
    OPEN_SESSION_STATUSES,
    ChatMessageRole,
    ChatMessageStatus,
    FrameDirection,
    SessionStatus,
    TurnOutcome,
)
from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import Session, SessionFrame
from haku.console.x.claude_code.frames import DELTA_FRAME_KIND
from haku.console.x.claude_code.testing.wire import (
    assistant,
    prompt,
    result,
    text_block,
    text_delta,
    tool_result,
    tool_use_block,
)
from haku.console.x.conftest import MCP_TOKEN, age_lease, attach_channel, lease_of, queued_for_the_room, runtime_config
from haku.console.x.frame_projection import projected
from haku.console.x.sandbox_claims import ProvisioningStep, provisioning_view
from haku.console.x.session_notifications import SessionNotifications
from haku.console.x.session_runtime import GOING_AWAY_CODE, RolloutRecorder, SessionService, _replaying
from haku.console.x.session_store import ADOPTION_GRACE, BridgeAuthentication, MatrixSession, SessionStore, SpaSession
from haku.console.x.testing.recording_claims import RecordingClaims
from haku.runtime.x.bridge.cli_client import ClaudeCli, FrameSink, ReceivedFrame, SentPrompt
from haku.runtime.x.bridge.protocol import NOT_ADMITTED_CODE, ClaudeMessage


def test_runtime_deployment_wiring_has_no_application_defaults() -> None:
    assert all(field.is_required() for field in ClaudeRuntimeConfig.model_fields.values())


def test_claude_environment_contains_placeholder_proxy_and_ca_only() -> None:
    config = runtime_config(ca_bundle="/ca/bundle.pem")

    assert config.claude_environment() == {
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
        return SentPrompt(command_uuid="fake-command", frame_seq=self.prompt_frame_seq)

    def replay(self) -> None:
        """Deliver the script with nothing having been asked, as the runner's replay window does.

        A resumed turn asks no question — its question was asked by a process that is gone — so a
        double that only speaks when spoken to could not stand in for one.
        """
        for frame_seq, frame in zip(self.frame_seqs or [self._number() for _ in self.script], self.script, strict=True):
            self.deliver(frame, frame_seq)

    def deliver(self, frame: dict[str, Any], frame_seq: int | None = None) -> None:
        self._queue.put_nowait(
            ReceivedFrame(payload=frame, frame_seq=self._number() if frame_seq is None else frame_seq)
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


_TOOL_USE_SCRIPT = [
    assistant(tool_use_block("toolu_01", "mcp__haku-console__haku-console__list_mcp_servers", {})),
    assistant(text_block("The Haku Console catalog is available.")),
    result(text="The Haku Console catalog is available."),
]


async def test_run_turn_preserves_assistant_message_boundaries_around_tool_use(
    chat_store, chat_service, operator_id
) -> None:
    """A tool-use block and the text after it are two messages, not one merged row."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "Check the Haku MCP catalog")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None

    client = _FakeCli(_TOOL_USE_SCRIPT)
    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, frontend=None, abort_event=asyncio.Event()
    )

    messages = [
        m for m in (await chat_store.get(operator_id, view.session_id)).messages if m.role == ChatMessageRole.ASSISTANT
    ]
    assert [(m.content, [u.model_dump() for u in m.tool_calls], m.status) for m in messages] == [
        (
            "",
            [
                {
                    "call_id": "toolu_01",
                    "tool_name": "mcp__haku-console__haku-console__list_mcp_servers",
                    "arguments": {},
                    # No `user` frame answered it in this test, and the view says so rather than
                    # showing an empty result.
                    "result": None,
                }
            ],
            ChatMessageStatus.COMPLETE,
        ),
        ("The Haku Console catalog is available.", [], ChatMessageStatus.COMPLETE),
    ]
    assert await chat_store.status(view.session_id) == SessionStatus.READY, "the turn was not completed"


async def test_projected_assistant_message_points_to_the_frames_that_built_it(
    chat_store, chat_service, operator_id
) -> None:
    """A message row keeps a navigable range into the lossless rollout rather than only a copy."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "say hello")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None

    # Recorded through the real sink, so the numbers the turn is handed are the rollout's own.
    script = [text_delta("hello"), assistant(text_block("hello")), result(text="hello")]
    recorder = RolloutRecorder(chat_store, view.session_id)
    prompt_frame_seq = await recorder.sent(prompt("say hello"))
    frame_seqs = [(await recorder.received(frame, runner_seq=None)).frame_seq for frame in script]

    client = _FakeCli(script, frame_seqs=frame_seqs, prompt_frame_seq=prompt_frame_seq)
    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, frontend=None, abort_event=asyncio.Event()
    )

    messages = (await chat_store.get(operator_id, view.session_id)).messages
    user_message = one(message for message in messages if message.role == ChatMessageRole.USER)
    assert (user_message.source_first_frame_seq, user_message.source_last_frame_seq) == (
        prompt_frame_seq,
        prompt_frame_seq,
    )
    # The delta opened the message and the `assistant` frame closed it; the `result` frame after
    # them ends the turn rather than the message, so it is deliberately outside the range.
    message = one(message for message in messages if message.role == ChatMessageRole.ASSISTANT)
    assert (message.source_first_frame_seq, message.source_last_frame_seq) == (frame_seqs[0], frame_seqs[1])


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

    The sink is not optional in the real client (<../../runtime/x/bridge/cli_client.py>): every
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
        self.prompt_frame_seq = await self._frames_to.sent(prompt(text))
        self.frame_seqs = [(await self._frames_to.received(frame, runner_seq=None)).frame_seq for frame in self.script]
        return await super().query(text)


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


async def test_session_lifecycle_creates_claim_accepts_bridge_and_disposes_claim(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    websocket = _LifecycleWebSocket()

    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    _ClosingClaudeClient.on_connect = lambda: chat_store.request_close(operator_id, session_id)
    with patch("haku.console.x.session_runtime.cli_over_websocket", _ClosingClaudeClient):
        await chat_service.handle_runner(cast(Any, websocket), session_id, recording_claims.tokens[session_id])

    assert recording_claims.created == [session_id]
    assert websocket.accepted is True
    assert websocket.closed is None
    assert recording_claims.deleted == [session_id]
    assert await chat_store.status(session_id) == SessionStatus.CLOSED
    # Cleanup is recorded by stamping `claim_cleaned_at`, which is what takes the session back out
    # of the reconciler's candidate set.
    assert await chat_store.claim_cleanup_candidates() == []
    # Asserted on the launch the runner is handed rather than on SDK options, since that is
    # what now crosses the wire — and it is where a bearer would leak if one ever did.
    launch = cast(Any, _ClosingClaudeClient.last_launch)
    assert json.loads(launch.arguments[launch.arguments.index("--mcp-config") + 1]) == {
        "mcpServers": {
            "haku-console": {
                "type": "http",
                "url": "http://haku-console.test:9090/mcp",
                "headers": {"Authorization": "Bearer haku-static-bearer"},
            }
        }
    }
    assert "--strict-mcp-config" in launch.arguments
    assert "haku-static-bearer" not in launch.environment.values()


class _RollingClaudeClient(_LifecycleClaudeClient):
    """Stands in for this replica being cancelled mid-session, which is what a roll is."""

    async def connect(self) -> dict[str, Any]:
        await super().connect()
        raise asyncio.CancelledError


async def test_a_rolling_replica_hands_the_session_back_instead_of_ending_it(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """A roll cancels `handle_runner`. Failing the row there refuses the runner's reconnect as
    terminal and replaces the whole session, which at six rolls a day is the ordinary end of a
    conversation."""
    websocket = _LifecycleWebSocket()

    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    with (
        patch("haku.console.x.session_runtime.cli_over_websocket", _RollingClaudeClient),
        pytest.raises(asyncio.CancelledError),
    ):
        await chat_service.handle_runner(cast(Any, websocket), session_id, recording_claims.tokens[session_id])

    assert await chat_store.status(session_id) == SessionStatus.READY, "a roll is not a session ending"
    assert recording_claims.deleted == [], "the sandbox outlives the replica that was serving it"
    assert websocket.closed == (GOING_AWAY_CODE, "console replica going away"), (
        "the runner reconnects because it was told to, not because it guessed"
    )


async def test_a_returning_runner_is_admitted_and_takes_the_lease(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """A runner whose replica went away is admitted by the next one, which keeps the sandbox."""
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    token = recording_claims.tokens[session_id]
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED

    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
        assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.HELD, (
            "a replica still renewing its lease keeps the session it is serving — but only until it lapses"
        )
        await chat_store.release_lease(session_id)
        assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED, (
            "a session handed back is adoptable by whichever replica the runner reaches"
        )


async def test_adoption_picks_the_answer_up_where_it_stopped(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """The runner replays what a console may not have recorded but never the deltas, so a resumed
    turn starting from an empty string would write the tail of the answer as a second message.
    Adoption says which turn; the turn's own row says how far it got.
    """
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await chat_store.enqueue_prompt(operator_id, session_id, "what were we doing")
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})
    assistant_id = await chat_store.begin_assistant(session_id, started.turn_id, source_first_frame_seq=1)
    await chat_store.update_assistant(session_id, assistant_id, "we were half way through")

    resumed = await chat_store.adopt_open_turn(session_id)

    assert resumed is not None
    state = await chat_store.turn_state(resumed.turn_id)
    assert (state.assistant_message_id, state.streamed) == (assistant_id, "we were half way through")
    assert not state.said_anything, "the message is still open, so nothing has completed"
    assert not state.queued_reply, "nothing completed, so the room has heard nothing to repeat"


async def test_a_turn_that_said_something_the_room_could_not_hear_still_knows_it_spoke(
    chat_store, chat_service, operator_id
) -> None:
    """A session with no room queues nothing, so `queued_reply` is false while `said_anything` is
    true. The resumed turn must read the second, or `result.result` — which repeats the message
    that already completed — becomes a message of its own.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    session_id = view.session_id
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, session_id, "why did it fail?")
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    assistant_id = await chat_store.begin_assistant(session_id, started.turn_id, source_first_frame_seq=1)
    assert not await chat_store.update_assistant(session_id, assistant_id, "a bad config", complete=True)
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})

    resumed = await chat_store.adopt_open_turn(session_id)
    assert resumed is not None
    assert (await chat_store.turn_state(resumed.turn_id)).said_anything

    client = _FakeCli([result(text="a bad config")])
    client.replay()
    async with asyncio.timeout(30):
        await chat_service._run_turn(
            cast(Any, client),
            client.frames().__aiter__(),
            session_id,
            resumed,
            frontend=None,
            abort_event=asyncio.Event(),
        )

    assistants = [m for m in (await chat_store.get(operator_id, session_id)).messages if m.role == "assistant"]
    assert [m.message_id for m in assistants] == [assistant_id], "the result frame repeated a message, not made one"


async def test_adoption_closes_a_turn_whose_result_nobody_projected(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """The exchange is over and its `result` sits past the session's cursor, so nothing acted on it.

    Waiting for it on the socket would wait forever — the runner replays that frame and
    `record_frame` refuses it as one this session already has — so adoption hands it back as a
    frame to project, and projecting it closes the turn through the loop a live frame goes through.
    """
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await chat_store.enqueue_prompt(operator_id, session_id, "what were we doing")
    assert await chat_store.next_prompt(session_id) is not None
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})
    await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, "result", result(uuid="res-1"))

    resumed = await chat_store.adopt_open_turn(session_id)
    assert resumed is not None
    assert [frame.payload["type"] for frame in resumed.replay] == ["user", "result"]
    client = _FakeCli()
    async with asyncio.timeout(30):
        await chat_service._run_turn(
            cast(Any, client),
            _replaying(resumed.replay, client.frames().__aiter__()),
            session_id,
            resumed,
            frontend=None,
            abort_event=asyncio.Event(),
        )

    [turn] = await chat_store.list_turns(str(session_id), cursor=None, limit=5)
    assert turn.outcome == TurnOutcome.ANSWERED


async def test_adoption_reads_a_failed_result_as_a_failed_turn(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """`is_error` is `false` on every production result, including all 27 sessions the console
    recorded as failed (<../debug/frame_shape_census.md>), so closing from it adopts a turn that
    ended badly as answered. The projection of the frame closes the turn instead, so recovery fails
    exactly as the live path fails on the same frame.
    """
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await chat_store.enqueue_prompt(operator_id, session_id, "what were we doing")
    assert await chat_store.next_prompt(session_id) is not None
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})
    await chat_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, "result", result(uuid="res-1", subtype="error_during_execution")
    )

    resumed = await chat_store.adopt_open_turn(session_id)
    assert resumed is not None
    client = _FakeCli()
    with pytest.raises(RuntimeError, match="error_during_execution"):
        async with asyncio.timeout(30):
            await chat_service._run_turn(
                cast(Any, client),
                _replaying(resumed.replay, client.frames().__aiter__()),
                session_id,
                resumed,
                frontend=None,
                abort_event=asyncio.Event(),
            )

    [turn] = await chat_store.list_turns(str(session_id), cursor=None, limit=5)
    assert turn.outcome == TurnOutcome.FAILED


async def test_a_turn_whose_cursor_is_behind_it_is_failed_rather_than_resumed(
    chat_store, chat_service, recording_claims, migrated_sessions, operator_id
) -> None:
    """A cursor from before the turn names a position this turn's writes never took, so resuming
    from it would redo effects that did commit — a duplicated message and a duplicated room reply.

    `next_prompt` anchors the cursor at the frame before the turn, so no session that can still
    acquire a frame is in this state; one that somehow is has its turn ended rather than resumed.

    The turn has to open past frame 1 for the state to be expressible at all: the anchor is
    `first_frame_seq - 1`, so a turn opening at 1 anchors at 0 — which is also "nothing has ever
    projected" — and there is no position below it to put the cursor.
    """
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, "system", {"type": "system"})
    await chat_store.enqueue_prompt(operator_id, session_id, "what were we doing")
    assert await chat_store.next_prompt(session_id) is not None
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})
    async with migrated_sessions() as db:
        await db.execute(update(Session).where(Session.session_id == session_id).values(projected_frame_seq=0))
        await db.commit()

    assert await chat_store.adopt_open_turn(session_id) is None

    [turn] = await chat_store.list_turns(str(session_id), cursor=None, limit=5)
    assert turn.outcome == TurnOutcome.FAILED


async def test_a_turn_that_never_asked_its_prompt_gives_it_back(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """`next_prompt` claims the prompt; `_run_turn` writes it afterwards. A replica dying between
    the two asked nothing, so the prompt is owed a second offer rather than a silent burial.
    """
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await chat_store.enqueue_prompt(operator_id, session_id, "what were we doing")
    claimed = await chat_store.next_prompt(session_id)
    assert claimed is not None

    assert await chat_store.adopt_open_turn(session_id) is None, "nothing to resume; nothing was asked"

    reoffered = await chat_store.next_prompt(session_id)
    assert reoffered is not None, "a prompt that never left is still waiting to be asked"
    assert reoffered.message_id == claimed.message_id
    assert reoffered.prompt == "what were we doing"
    assert reoffered.turn_id != claimed.turn_id


async def test_a_turn_that_asked_its_prompt_keeps_it(chat_store, chat_service, recording_claims, operator_id) -> None:
    """The agent has it and the runner will replay its answer, so re-offering would ask twice."""
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await chat_store.enqueue_prompt(operator_id, session_id, "what were we doing")
    assert await chat_store.next_prompt(session_id) is not None
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})

    assert await chat_store.adopt_open_turn(session_id) is not None

    assert await chat_store.next_prompt(session_id) is None, "the queue has nothing; the turn has it"


async def test_a_held_session_tells_the_runner_to_retry_rather_than_refusing_it(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """A runner redials about a second after its socket drops, so it routinely reaches a new replica
    while the dying one's lease is still valid. Closing before `accept()` reaches it as 403 whatever
    code is passed, and 403 is a refusal it correctly gives up on — costing the sandbox. 503 is what
    it waits out."""
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    token = recording_claims.tokens[session_id]
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    websocket = _LifecycleWebSocket()

    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
        await chat_service.handle_runner(cast(Any, websocket), session_id, token)

    assert websocket.denied == 503
    assert websocket.closed is None, "a close before accept is the 403 this exists to avoid"
    assert not websocket.accepted


async def test_a_bad_credential_is_still_refused_outright(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """The other side of the distinction: a runner that will never be admitted must not spend its
    redial budget finding that out."""
    session = await chat_service.create(operator_id, SpaSession())
    websocket = _LifecycleWebSocket()

    await chat_service.handle_runner(cast(Any, websocket), session.session_id, "wrong")

    assert websocket.denied is None
    assert websocket.closed == (NOT_ADMITTED_CODE, "invalid or consumed runner credential")


async def test_terminal_runner_retry_deletes_its_stale_claim(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """A runner presenting a valid credential for an already-closed session is turned away."""
    websocket = _LifecycleWebSocket()

    session = await chat_service.create(operator_id, SpaSession())
    await chat_store.request_close(operator_id, session.session_id)

    await chat_service.handle_runner(
        cast(Any, websocket), session.session_id, recording_claims.tokens[session.session_id]
    )

    assert recording_claims.deleted == [session.session_id]
    assert await chat_store.claim_cleanup_candidates() == []
    assert await chat_store.status(session.session_id) == SessionStatus.CLOSED
    assert websocket.closed == (1008, "runner session is already terminal")


async def test_startup_reconciliation_retries_terminal_claim_cleanup(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """Claims left behind by a Console that died mid-teardown are swept on the next boot."""

    session_ids = []
    for _ in range(2):
        session = await chat_service.create(operator_id, SpaSession())
        await chat_store.request_close(operator_id, session.session_id)
        session_ids.append(session.session_id)

    await chat_service.reconcile_terminal_claims()

    assert sorted(recording_claims.deleted) == sorted(session_ids)
    assert await chat_store.claim_cleanup_candidates() == []


ROOM = "!room:example.org"

_NARRATED_TURN = [
    assistant(text_block("Looking at the logs now.")),
    assistant(tool_use_block("toolu_01", "Bash", {"command": "true"})),
    assistant(text_block("Found it: a bad config.")),
    result(text="Found it: a bad config."),
]


class _RecordingFrontend:
    """A `ChatFrontend` that keeps what it was told instead of talking to a homeserver.

    Answers are not among it: they are `session_outbox` rows, so what the room is owed is read out
    of the database (`queued_for_the_room`) rather than out of a sink the turn calls.
    """

    def __init__(self) -> None:
        self.silent_turns = 0

    async def system_prompt(self, session_id: UUID) -> str:
        return "you are Haku"

    async def report_silent_turn(self) -> None:
        self.silent_turns += 1

    async def report(self, detail: str) -> None:
        return None

    async def show_status(self, text: str) -> None:
        return None

    async def clear_status(self) -> None:
        return None

    async def set_typing(self, active: bool) -> None:
        return None


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
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    recording_claims: RecordingClaims,
    notifications: SessionNotifications,
    operator_id: UUID,
    client: _FakeCli,
    *,
    abort_event: asyncio.Event | None = None,
    frontend: _RecordingFrontend | None = None,
) -> list[str]:
    """Run one turn against *client* for a room-backed session and return what the room is owed."""
    frontend = frontend or _RecordingFrontend()
    service = SessionService(
        runtime_config(), chat_store, recording_claims, notifications, mcp_token=MCP_TOKEN, chat_frontend=frontend
    )
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    await attach_channel(migrated_sessions, view.session_id, ROOM)
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    async with asyncio.timeout(30):
        await service._run_turn(
            client,
            client.frames().__aiter__(),
            view.session_id,
            turn,
            frontend=frontend,
            abort_event=abort_event or asyncio.Event(),
        )
    return await queued_for_the_room(migrated_sessions, view.session_id)


async def test_only_the_sessions_that_serve_a_room_are_attached_to_the_frontend(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """One console serves both surfaces, and the frontend is bound to its room — so which sessions
    it speaks for is whether a channel holds a copy of the thread they run, read once per
    connection."""
    frontend = _RecordingFrontend()
    service = SessionService(
        runtime_config(), chat_store, recording_claims, notifications, mcp_token=MCP_TOKEN, chat_frontend=frontend
    )
    spa, _ = await chat_store.create(operator_id, SpaSession())
    room_backed, _ = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    await attach_channel(migrated_sessions, room_backed.session_id, ROOM)

    assert (await service._frontend_for(spa.session_id), await service._frontend_for(room_backed.session_id)) == (
        None,
        frontend,
    )


async def test_a_resumed_turn_finishes_the_answer_it_inherited(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """The replacement replica finishes the exchange the dead one started, in the message it
    started, and the room is owed the answer once."""
    frontend = _RecordingFrontend()
    service = SessionService(
        runtime_config(), chat_store, recording_claims, notifications, mcp_token=MCP_TOKEN, chat_frontend=frontend
    )
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    session_id = view.session_id
    await attach_channel(migrated_sessions, session_id, ROOM)
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, session_id, "why did it fail?")
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    # What the previous holder got through before its pod went: the prompt written, and half an
    # answer streamed into a message it never closed — applied as the loop applies any frame, so
    # the message row, the turn's pointer at it and the session's cursor all landed together.
    delta = text_delta("because the ")
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})
    opened_at = await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, "stream_event", delta)
    state = await chat_store.apply_frame(
        session_id, started.turn_id, opened_at.frame_seq, projected(frame_seq=opened_at.frame_seq, payload=delta)
    )
    assistant_id = state.assistant_message_id

    resumed = await chat_store.adopt_open_turn(session_id)
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
            frontend=frontend,
            abort_event=asyncio.Event(),
        )

    queued = await queued_for_the_room(migrated_sessions, session_id)
    assert queued == ["because the disk was full"], "one row, not the answer twice"
    assistants = [m for m in (await chat_store.get(operator_id, session_id)).messages if m.role == "assistant"]
    assert [m.message_id for m in assistants] == [assistant_id], "continued, rather than forked into a second"
    [turn] = await chat_store.list_turns(str(session_id), cursor=None, limit=5)
    assert (turn.turn_id, turn.outcome) == (started.turn_id, TurnOutcome.ANSWERED)


async def test_adoption_redoes_the_frames_past_the_cursor_and_only_those(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """Two frames are in the log and the departed holder projected one of them.

    The cursor is the whole of what tells them apart, and both errors it prevents are visible from
    the room: redoing the projected delta would append its prose to the message a second time,
    while not redoing the unprojected answer would lose it outright — the runner will not offer a
    frame this session already recorded, so nothing else is coming to write it down.
    """
    frontend = _RecordingFrontend()
    service = SessionService(
        runtime_config(), chat_store, recording_claims, notifications, mcp_token=MCP_TOKEN, chat_frontend=frontend
    )
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    session_id = view.session_id
    await attach_channel(migrated_sessions, session_id, ROOM)
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, session_id, "why did it fail?")
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    delta = text_delta("because the ")
    answer = assistant(text_block("because the disk was full"))
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})
    recorded = await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, "stream_event", delta)
    await chat_store.apply_frame(
        session_id, started.turn_id, recorded.frame_seq, projected(frame_seq=recorded.frame_seq, payload=delta)
    )
    # Recorded and then nothing: the pod went between the sink writing the row and the loop acting
    # on what it meant.
    unprojected = await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, "assistant", answer)

    resumed = await chat_store.adopt_open_turn(session_id)
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
            frontend=frontend,
            abort_event=asyncio.Event(),
        )

    assert await queued_for_the_room(migrated_sessions, session_id) == ["because the disk was full"]
    assistants = [m for m in (await chat_store.get(operator_id, session_id)).messages if m.role == "assistant"]
    assert [m.content for m in assistants] == ["because the disk was full"]


async def test_a_resumed_turn_does_not_say_again_what_it_had_already_queued(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """The departed holder finished a message and the room's outbox holds it. All the replacement
    sees is the `result` frame — which repeats that same text — so it has to know the room is
    already owed it. `queued_reply` is that, written by the transaction that inserted the row
    rather than inferred from an `assistant` frame having been recorded.
    """
    frontend = _RecordingFrontend()
    service = SessionService(
        runtime_config(), chat_store, recording_claims, notifications, mcp_token=MCP_TOKEN, chat_frontend=frontend
    )
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    session_id = view.session_id
    await attach_channel(migrated_sessions, session_id, ROOM)
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, session_id, "why did it fail?")
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})
    assistant_id = await chat_store.begin_assistant(session_id, started.turn_id, source_first_frame_seq=1)
    assert await chat_store.update_assistant(session_id, assistant_id, "a bad config", complete=True)

    resumed = await chat_store.adopt_open_turn(session_id)
    assert resumed is not None
    client = _FakeCli([result(text="a bad config")])
    client.replay()
    async with asyncio.timeout(30):
        await service._run_turn(
            cast(Any, client),
            client.frames().__aiter__(),
            session_id,
            resumed,
            frontend=frontend,
            abort_event=asyncio.Event(),
        )

    assert await queued_for_the_room(migrated_sessions, session_id) == ["a bad config"], "the answer, once"


async def test_the_room_is_owed_each_assistant_message_as_it_finishes(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """A turn that says what it is about to do, works, then reports back is three messages in the
    transcript, and the room gets all three rather than only the conclusion."""
    queued = await _turn_into_a_room(
        chat_store, migrated_sessions, recording_claims, notifications, operator_id, _FakeCli(_NARRATED_TURN)
    )

    assert queued == ["Looking at the logs now.", "Found it: a bad config."]


async def test_the_last_message_is_not_repeated_by_the_result_frame(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """`result.result` carries the same text as the turn's last assistant message, so queueing
    both would post the answer twice."""
    queued = await _turn_into_a_room(
        chat_store, migrated_sessions, recording_claims, notifications, operator_id, _FakeCli(_NARRATED_TURN)
    )

    assert queued.count("Found it: a bad config.") == 1


async def test_the_room_is_owed_the_answer_before_the_turn_can_fail(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """The drop that needs neither a reconnection nor a roll: a turn that raised after producing
    text (<../debug/message_drops.md> E4). The failing `result` raises before any delivery ran, so
    the outbox row has to be written with the message, in one transaction, for the answer to
    outlive the turn that produced it.
    """
    frontend = _RecordingFrontend()
    service = SessionService(
        runtime_config(), chat_store, recording_claims, notifications, mcp_token=MCP_TOKEN, chat_frontend=frontend
    )
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    await attach_channel(migrated_sessions, view.session_id, ROOM)
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    client = _FakeCli([*_NARRATED_TURN[:-1], result(subtype="error_during_execution", is_error=True)])

    with pytest.raises(RuntimeError):
        async with asyncio.timeout(30):
            await service._run_turn(
                client,
                client.frames().__aiter__(),
                view.session_id,
                turn,
                frontend=frontend,
                abort_event=asyncio.Event(),
            )

    assert await queued_for_the_room(migrated_sessions, view.session_id) == [
        "Looking at the logs now.",
        "Found it: a bad config.",
    ]


async def test_a_turn_the_cli_ended_badly_fails_even_though_is_error_says_it_did_not(
    chat_store, chat_service, operator_id
) -> None:
    """`is_error` is false on all 129 production `result` frames — including every one of the 27
    sessions the console recorded as failed — so a loop reading it calls every turn fine. The turn's
    outcome is the projection's, and that reads `subtype` (<claude_code/projection.py>).
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "keep going")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    client = _FakeCli([result(subtype="error_max_turns")])

    with pytest.raises(RuntimeError, match="error_max_turns"):
        await chat_service._run_turn(
            client, client.frames().__aiter__(), view.session_id, turn, frontend=None, abort_event=asyncio.Event()
        )

    [record] = await chat_store.list_turns(view.session_id, cursor=None, limit=5)
    assert record.outcome == TurnOutcome.FAILED


async def test_a_turn_whose_answer_arrived_only_on_the_result_is_still_spoken(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """No assistant message completed, so nothing was said along the way — the `result` frame is
    the only thing that keeps the room from hearing silence."""
    queued = await _turn_into_a_room(
        chat_store,
        migrated_sessions,
        recording_claims,
        notifications,
        operator_id,
        _FakeCli([result(text="nothing streamed, but an answer")]),
    )

    assert queued == ["nothing streamed, but an answer"]


async def test_a_turn_with_nothing_at_all_to_say_reports_it_rather_than_queueing_nothing(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """There is no silence token, and an empty answer is not one: the room is told the turn
    finished without saying anything, as a notice, and no row is written for the empty string."""
    frontend = _RecordingFrontend()

    queued = await _turn_into_a_room(
        chat_store,
        migrated_sessions,
        recording_claims,
        notifications,
        operator_id,
        _FakeCli([result(text="")]),
        frontend=frontend,
    )

    assert (queued, frontend.silent_turns) == ([], 1)


async def test_an_aborted_turn_leaves_a_notice_and_no_reply(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """Two things. The operator's stop reaches the room as a notice and nothing else — no
    `session_outbox` row, because the fact is a `session_events` row and the notice is its
    projection. And the turn has to *survive* the abort: draining to the interrupt's `result` must
    not open a second `anext` on the session's generator, which an async generator refuses, since
    an abort lands exactly there — between frames.
    """
    abort_event = asyncio.Event()
    client = _InterruptedCli(_NARRATED_TURN[:-1], abort_event=abort_event)
    frontend = _RecordingFrontend()

    queued = await _turn_into_a_room(
        chat_store,
        migrated_sessions,
        recording_claims,
        notifications,
        operator_id,
        client,
        abort_event=abort_event,
        frontend=frontend,
    )

    assert client.interrupted
    # The two messages, and nothing from the interrupt's own `result` frame ("stopped"). That the
    # stop itself is recorded is `test_session_store`'s; the room reads that row for itself
    # (<channels/matrix/room_subscription.py>) rather than being told here.
    assert queued == ["Looking at the logs now.", "Found it: a bad config."]


async def test_an_abort_mid_answer_leaves_the_half_answer_unmarked(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """Stopped between deltas, so no assistant message ever completed: the message row that closes
    the stream carries the half answer and only that. The stop is the console's fact, not part of
    what the agent said, so it does not get written into the agent's words.
    """
    abort_event = asyncio.Event()
    client = _InterruptedCli([text_delta("because the "), text_delta("disk was full")], abort_event=abort_event)
    frontend = _RecordingFrontend()

    queued = await _turn_into_a_room(
        chat_store,
        migrated_sessions,
        recording_claims,
        notifications,
        operator_id,
        client,
        abort_event=abort_event,
        frontend=frontend,
    )

    assert queued == ["because the disk was full"]


async def test_a_message_the_agent_finished_before_stopping_survives_the_drain(
    chat_store, migrated_sessions, recording_claims, notifications, operator_id
) -> None:
    """<../debug/message_drops.md> E3 — the drop an outbox cannot close, because the reply never
    reaches the delivery layer at all.

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
        chat_store, migrated_sessions, recording_claims, notifications, operator_id, client, abort_event=abort_event
    )

    assert client.interrupted
    # The drained message once, and nothing from the interrupt's own `result` frame ("stopped"),
    # which the finished message is what makes redundant.
    assert queued == ["Looking at the logs now.", "Found it: a bad config."]


async def test_a_turn_brackets_the_frames_it_produced(chat_store, chat_service, operator_id) -> None:
    """The bracket is what makes a turn's own frames findable afterwards."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    # A frame from before this turn, so a bracket that started at the log's beginning would show.
    await chat_store.record_frame(view.session_id, FrameDirection.FROM_AGENT, "system", {"type": "system"})
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    answer = assistant(text_block("a bad config"))
    ending = result(text="a bad config")
    # In production the socket wrapper writes these, so the recorder and the turn loop see the same
    # frames; here the double is handed the numbers the log gave them.
    recorded_answer = await chat_store.record_frame(view.session_id, FrameDirection.FROM_AGENT, "assistant", answer)
    recorded_ending = await chat_store.record_frame(view.session_id, FrameDirection.FROM_AGENT, "result", ending)
    client = _FakeCli([answer, ending], frame_seqs=[recorded_answer.frame_seq, recorded_ending.frame_seq])

    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, frontend=None, abort_event=asyncio.Event()
    )

    [record] = await chat_store.list_turns(view.session_id, cursor=None, limit=10)
    assert record.outcome == TurnOutcome.ANSWERED
    assert (record.first_frame_seq, record.last_frame_seq) == (recorded_answer.frame_seq, recorded_ending.frame_seq)
    assert record.ended_at is not None


async def test_a_turn_ends_at_its_own_result_rather_than_at_what_the_cli_logs_after_it(
    chat_store, chat_service, operator_id
) -> None:
    """The CLI emits a `command_lifecycle` frame just after the `result` one, so it is already in
    the log by the time the turn loop closes the turn, and a bound taken from the log's head reports
    it as the turn's last frame — on 80 of 99 production turns (2026-08-16)."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    ending = result(text="a bad config")
    recorded = await chat_store.record_frame(view.session_id, FrameDirection.FROM_AGENT, "result", ending)
    await chat_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, "command_lifecycle", {"type": "command_lifecycle"}
    )

    client = _FakeCli([ending], frame_seqs=[recorded.frame_seq])

    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, frontend=None, abort_event=asyncio.Event()
    )

    [record] = await chat_store.list_turns(view.session_id, cursor=None, limit=10)
    assert record.last_frame_seq == recorded.frame_seq


async def test_the_transcript_carries_what_each_tool_answered(chat_store, chat_service, operator_id) -> None:
    """The call and its answer are both `session_events` rows, paired by `call_id` — exact, where
    matching the Nth message to the Nth frame would be a guess."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "count the files")
    turn = await chat_store.next_prompt(view.session_id)
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
        client, client.frames().__aiter__(), view.session_id, turn, frontend=None, abort_event=asyncio.Event()
    )

    calls = {
        call.call_id: call
        for message in (await chat_store.get(operator_id, view.session_id)).messages
        for call in message.tool_calls
    }

    assert calls["toolu_ok"].result is not None
    assert (calls["toolu_ok"].result.content, calls["toolu_ok"].result.is_error) == ("42", False)
    assert calls["toolu_running"].result is None, "a call still running must not read as an empty answer"


async def test_the_calls_come_from_the_events_and_need_no_id_from_the_agent(
    chat_store, chat_service, operator_id
) -> None:
    """A message finds its calls through the frames it was built from, and nothing else.

    1,417 production assistant rows carry no `agent_message_id`, and this frame carries none
    either, so the frame range is the whole of what pairs them.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "count the files")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    client = _FakeCli(
        [
            assistant(tool_use_block("toolu_ok", "Bash", {"command": "true"})),
            tool_result("toolu_ok", "7"),
            result(text="done"),
        ]
    )
    await chat_service._run_turn(
        client, client.frames().__aiter__(), view.session_id, turn, frontend=None, abort_event=asyncio.Event()
    )
    [call] = [
        call for message in (await chat_store.get(operator_id, view.session_id)).messages for call in message.tool_calls
    ]

    assert (call.call_id, call.tool_name) == ("toolu_ok", "Bash")
    assert call.result is not None
    assert call.result.content == "7"


class _RealDbClaudeClient(_LifecycleClaudeClient):
    """Answers every prompt with "pong", then goes quiet like an idle CLI."""

    def __init__(self, adapter: object, launch: object, on_progress: object, frames_to: FrameSink):
        super().__init__(adapter, launch, on_progress, frames_to)
        self.script = [assistant(text_block("pong")), result(text="pong")]


async def test_runner_survives_an_idle_wait_against_a_real_database(chat_store, chat_service, operator_id) -> None:
    """The idle wait is a raw-driver call, so only a real engine exercises it.

    `handle_runner` loops: consume a prompt, then block in `wait_for_prompt` until the next one.
    That wait talks to `driver_connection` directly, so a driver-API mismatch there is invisible to
    any test that fakes the store — and it killed every Matrix session about four seconds in with
    "'Connection' object has no attribute 'set_autocommit'".
    """
    # The store mints the real bridge token; no claim is created because handle_runner only ever
    # deletes one on the way out, and Kubernetes is not what this test is about.
    view, token = await chat_store.create(operator_id, SpaSession())

    with patch("haku.console.x.session_runtime.cli_over_websocket", _RealDbClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        try:
            # Long enough to reach the idle wait, which is where the crash used to happen.
            await asyncio.sleep(2)
            assert await chat_store.status(view.session_id) == SessionStatus.READY, (
                "the runner failed while waiting for a prompt"
            )

            # And the wait must actually wake on NOTIFY rather than only time out. A bounded poll
            # rather than an Event, so the runner's wake is observed from outside; what it polls
            # for is the closed turn, since the session's status stays `ready` throughout.
            await chat_store.enqueue_prompt(operator_id, view.session_id, "ping")
            for _ in range(75):
                if [
                    turn
                    for turn in await chat_store.list_turns(str(view.session_id), cursor=None, limit=2)
                    if turn.ended_at
                ]:
                    break
                await asyncio.sleep(0.2)
            [turn] = await chat_store.list_turns(str(view.session_id), cursor=None, limit=2)
            assert turn.outcome == TurnOutcome.ANSWERED, "the turn never completed"
        finally:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    [answer] = [
        m for m in (await chat_store.get(operator_id, view.session_id)).messages if m.role == ChatMessageRole.ASSISTANT
    ]
    assert answer.content == "pong"


class _ScriptedChannel:
    """A `FrameChannel` whose far end is a queue of the CLI's own frames."""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self._inbound: asyncio.Queue[ClaudeMessage | None] = asyncio.Queue()
        self._wrote = asyncio.Event()

    def deliver(self, frame: dict[str, Any], *, seq: int | None = None) -> None:
        self._inbound.put_nowait(ClaudeMessage(payload=frame, seq=seq))

    async def connect(self) -> None:
        pass

    async def write(self, data: str) -> None:
        self.written.append(json.loads(data))
        self._wrote.set()

    async def first_write(self) -> dict[str, Any]:
        """The opening frame, once it is actually on the wire.

        `cli_client._write` numbers a frame before writing it, and numbering here is a database
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
    return "".join(frame.payload["event"]["delta"]["text"] for frame in frames if frame.kind == DELTA_FRAME_KIND)


async def test_the_rollout_records_both_channels_both_ways_and_skips_only_deltas(
    chat_store, migrated_sessions, operator_id
) -> None:
    """What the agent did is only recoverable from the wire.

    Tool results arrive as `user` frames, which the turn loop drops entirely, so the record is
    taken where every frame passes rather than from what the loop unpacks. **The control channel
    counts.** It never reaches `frames()`, so recording off the conversation queue would drop
    `interrupt` and its answer, and an interrupt that did not take is diagnosable from nothing else.
    """
    view, _ = await chat_store.create(operator_id, SpaSession())
    answered = tool_result("toolu_1", "42")
    channel = _ScriptedChannel()
    cli = ClaudeCli(channel, RolloutRecorder(chat_store, view.session_id), control_timeout=5)

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
    assert delta_received.payload["type"] == "stream_event"
    result_received = await anext(frames)
    assert result_received.payload == answered
    await cli.aclose()

    # Every frame either way and no exceptions left — the delta included, which is what makes
    # this a log rather than a selection.
    recorded = await _frames(migrated_sessions, view.session_id)
    assert [(frame.direction, frame.kind) for frame in recorded] == [
        (FrameDirection.TO_AGENT, "control_request"),
        (FrameDirection.FROM_AGENT, "control_response"),
        (FrameDirection.TO_AGENT, "user"),
        (FrameDirection.FROM_AGENT, "stream_event"),
        (FrameDirection.FROM_AGENT, "user"),
    ]
    # Verbatim: a reader gets the tool result the turn loop never kept.
    assert recorded[4].payload == answered
    # Each frame reaches its consumer carrying the row it was written to, so a projection built
    # from it can point back at that row and not at whichever frame the reader has since seen.
    assert [delta_received.frame_seq, result_received.frame_seq] == [recorded[3].frame_seq, recorded[4].frame_seq]


async def test_the_runners_number_is_recorded_beside_the_rows_own(chat_store, migrated_sessions, operator_id) -> None:
    """Two numbers per row. `frame_seq` is Postgres's and is the log's ordering; `runner_seq` is
    the peer's and the only one a reconnect can hand back, which is what `highest_runner_seq`
    reads. A write to the CLI carries none: the runner numbers what it sends, not what it forwards.
    """
    view, _ = await chat_store.create(operator_id, SpaSession())
    channel = _ScriptedChannel()
    cli = ClaudeCli(channel, RolloutRecorder(chat_store, view.session_id), control_timeout=5)

    connecting = asyncio.create_task(cli.connect())
    initialize = await channel.first_write()
    channel.deliver(
        {"type": "control_response", "response": {"subtype": "success", "request_id": initialize["request_id"]}}, seq=11
    )
    await connecting
    channel.deliver(result(uuid="turn-1"), seq=12)
    assert (await anext(cli.frames())).payload["type"] == "result"
    await cli.aclose()

    recorded = await _frames(migrated_sessions, view.session_id)
    assert [(frame.kind, frame.runner_seq) for frame in recorded] == [
        ("control_request", None),
        ("control_response", 11),
        ("result", 12),
    ]
    assert await chat_store.highest_runner_seq(view.session_id) == 12


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
    chat_store, chat_service, migrated_sessions, operator_id
) -> None:
    """A roll drops the runner's socket while the session is between turns. It has to hand back
    then, rather than sit in the 30s prompt-wait until graceful shutdown cancels it: the connection
    watcher turns the drop into the disconnect the handler releases on. The proof is that the task
    ends on its own, with no cancel, and the session stays adoptable.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    _DisconnectingClaudeClient.instance = None

    with patch("haku.console.x.session_runtime.cli_over_websocket", _DisconnectingClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        for _ in range(75):
            if (
                _DisconnectingClaudeClient.instance is not None
                and await chat_store.status(view.session_id) == SessionStatus.READY
            ):
                break
            await asyncio.sleep(0.1)
        assert _DisconnectingClaudeClient.instance is not None
        assert await chat_store.status(view.session_id) == SessionStatus.READY

        _DisconnectingClaudeClient.instance.disconnect()
        await asyncio.wait_for(runner, timeout=5)

    assert await chat_store.status(view.session_id) in OPEN_SESSION_STATUSES, "handed back, not failed"
    holder, expires_at = await lease_of(migrated_sessions, view.session_id)
    assert holder is None
    assert expires_at <= datetime.now(UTC)


async def test_an_answer_cut_off_mid_stream_is_in_the_rollout(
    chat_store, chat_service, migrated_sessions, operator_id
) -> None:
    """The deltas are the record, and each is written as it crosses the wire, so a turn no
    `assistant` frame ever completed still has its half-answer in the log. A finalizer could not
    reconstruct it: a replica losing its pod raises `CancelledError` straight past one.
    """
    view, token = await chat_store.create(operator_id, SpaSession())

    with patch("haku.console.x.session_runtime.cli_over_websocket", _DyingMidStreamClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        try:
            for _ in range(75):
                if await chat_store.status(view.session_id) == SessionStatus.READY:
                    break
                await asyncio.sleep(0.2)
            await chat_store.enqueue_prompt(operator_id, view.session_id, "go")
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
    assert not [frame for frame in recorded if frame.kind == "assistant"], "no frame completed the message"


async def test_a_returning_runner_beats_the_sweep(
    chat_store, chat_service, recording_claims, operator_id, migrated_sessions
) -> None:
    """A runner that redials inside the adoption window is admitted, and the session keeps running
    under its new holder rather than being failed."""
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    token = recording_claims.tokens[session_id]
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await age_lease(migrated_sessions, session_id, seconds_ago=1)

    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
        assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED, (
            "a lapsed lease is adoptable by whichever replica the runner reaches"
        )

    assert await chat_store.expire_stale_leases() == 0
    assert await chat_store.status(session_id) in OPEN_SESSION_STATUSES


async def test_the_lease_heartbeat_also_slides_the_sandbox_deadline(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """The sandbox is a renewed lease, not a fixed timer: the heartbeat that renews the console
    lease also pushes the SandboxClaim's deadline out, so an active session is not reaped at
    `session_ttl_seconds`."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

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
    chat_store, recording_claims, chat_service, migrated_sessions, operator_id
) -> None:
    """A runner attached, then its lease was handed back (a roll, or the sandbox reaching its TTL)
    and no runner returned. `release` clears `lease_holder`, so the reason must not fall through to
    "never attached" for a session that was attached for hours."""
    session = await chat_service.create(operator_id, SpaSession())
    token = recording_claims.tokens[session.session_id]
    assert await chat_store.authenticate_bridge(session.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.release_lease(session.session_id)
    await age_lease(migrated_sessions, session.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1
    error = (await chat_store.get(operator_id, session.session_id)).error
    assert "never attached" not in error, "it was attached; the runner went away"
    assert "runner went away" in error


async def test_a_cancelled_runner_hands_the_session_back_without_stranding_it(
    chat_store, chat_service, migrated_sessions, operator_id
) -> None:
    """Pod termination cancels this task, and `CancelledError` is not an `Exception`, so neither
    `except` clause sees it. Neither answer at the two extremes works: leaving the row live strands
    a session nobody maintains, and failing it is terminal, which refuses the runner's reconnect and
    costs every roll its conversation. Handing it back keeps both properties — adoptable by
    whichever replica the runner reaches, and still caught by the sweep once the window passes.
    """
    view, token = await chat_store.create(operator_id, SpaSession())

    with patch("haku.console.x.session_runtime.cli_over_websocket", _RealDbClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        await asyncio.sleep(2)  # Long enough to reach the idle wait, as the sibling test does.
        assert await chat_store.status(view.session_id) == SessionStatus.READY

        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    assert await chat_store.status(view.session_id) == SessionStatus.READY
    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
        assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)
    assert await chat_store.expire_stale_leases() == 1
    assert await chat_store.status(view.session_id) == SessionStatus.FAILED


async def _force_status(sessions: async_sessionmaker[AsyncSession], session_id: UUID, status: SessionStatus) -> None:
    """Put a session in *status* directly.

    `idle` has no writer yet (see `SessionStatus.IDLE`) — it is the state a session sits in once a
    prompt rather than its creation buys the sandbox — so a test about it writes the row itself.
    """
    async with sessions.begin() as db:
        await db.execute(update(Session).where(Session.session_id == session_id).values(status=status))


async def test_a_session_that_never_asked_for_a_sandbox_reports_nothing_and_asks_kubernetes_nothing(
    chat_service, recording_claims, migrated_sessions, operator_id
) -> None:
    """No claim exists until allocation makes one, so an idle session's "nothing to report" is
    provable from the row — and must stay free, being the state a room nobody has spoken in sits
    in."""
    session = await chat_service.create(operator_id, SpaSession())
    await _force_status(migrated_sessions, session.session_id, SessionStatus.IDLE)

    view = await chat_service.sandbox_provisioning(operator_id, session.session_id)

    assert (view.status, view.sandbox) == (SessionStatus.IDLE, None)
    assert recording_claims.inspected == [], "an idle session has no claim to read"


async def test_a_session_that_failed_to_come_up_still_says_what_it_was_stuck_behind(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """The reason to ask a session that is no longer provisioning: the conversation read answers
    `null` for a failed session, which is the one that most needs to be asked why."""
    session = await chat_service.create(operator_id, SpaSession())
    recording_claims.answer(
        provisioning_view(
            f"claude-{session.session_id.hex}",
            step=ProvisioningStep.WAITING_FOR_POD,
            claim_ready=False,
            claim_message="no warm sandbox available",
        )
    )
    await chat_store.fail(session.session_id, "sandbox provisioning failed")

    view = await chat_service.sandbox_provisioning(operator_id, session.session_id)

    assert view.status is SessionStatus.FAILED
    assert view.sandbox is not None
    assert view.sandbox.step is ProvisioningStep.WAITING_FOR_POD
    assert view.sandbox.claim_message == "no warm sandbox available"


async def test_a_reclaimed_claim_is_not_the_same_answer_as_never_having_asked(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """`_cleanup_terminal_claim` deletes the claim once a session ends, so the cluster has nothing
    to show — which is a different answer from an idle session's, and neither of them is `null`."""
    session = await chat_service.create(operator_id, SpaSession())
    recording_claims.answer(provisioning_view(f"claude-{session.session_id.hex}", step=ProvisioningStep.CLAIM_ABSENT))
    await chat_store.closed(session.session_id)

    view = await chat_service.sandbox_provisioning(operator_id, session.session_id)

    assert view.status is SessionStatus.CLOSED
    assert view.sandbox is not None, "a claim that is gone is a fact, not an absence of one"
    assert view.sandbox.step is ProvisioningStep.CLAIM_ABSENT


async def test_a_cluster_that_cannot_be_read_says_so_instead_of_failing_the_request(
    chat_service, recording_claims, operator_id
) -> None:
    """The third of the three answers: not "nothing here" and not "the claim is gone", but "I could
    not look" — which is the one a reader must not act on."""
    session = await chat_service.create(operator_id, SpaSession())
    recording_claims.fail(RuntimeError("kubernetes: connection refused"))

    view = await chat_service.sandbox_provisioning(operator_id, session.session_id)

    assert view.sandbox is not None
    assert view.sandbox.observation_error == "kubernetes: connection refused"


async def test_polling_provisioning_reads_the_cluster_at_a_bounded_rate(
    chat_service, recording_claims, operator_id
) -> None:
    """One poll is up to three Kubernetes reads, and the browser's refresh rate is not the API
    server's problem — so polls inside one observation's budget cost one look at the cluster."""
    session = await chat_service.create(operator_id, SpaSession())

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
    session = await chat_service.create(operator_id, SpaSession())

    with pytest.raises(KeyError):
        await chat_service.sandbox_provisioning(uuid4(), session.session_id)


if __name__ == "__main__":
    pytest_bazel.main()
