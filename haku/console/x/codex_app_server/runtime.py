"""Codex app-server's provider-specific Console runtime adapter.

The shared Console loop supplies neutral launch facts and durable projection seeds.  This adapter
turns those into Codex's process launch, JSON-RPC thread configuration, connected client and native
frame reducer.  No Codex method or item discriminator escapes this package.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from haku.console.chat_models import RuntimeKind, TurnOutcome
from haku.console.x.codex_app_server import frames, projection
from haku.console.x.codex_app_server.client import CodexClientFactory, CodexThread, app_server_over_websocket
from haku.console.x.conversation_events import Projection, TurnCompleted
from haku.console.x.runtime import (
    EMPTY_TURN_PROJECTION_SEED,
    FrameEffects,
    OpenItemSeed,
    RuntimeClient,
    RuntimeLaunch,
    RuntimeTurnHandler,
    TurnCompletion,
    TurnProjectionSeed,
)
from haku.runtime.x.bridge.client import FrameSink
from haku.runtime.x.bridge.codex_options import CodexAppServerSession, HttpMcpServer, build_codex_launch
from haku.runtime.x.bridge.protocol import HarnessFrame, HarnessLaunch, TextWebSocket
from haku.runtime.x.bridge.transport import ProgressSink


@dataclass(frozen=True, slots=True)
class _NativeLaunch:
    harness: HarnessLaunch
    thread: CodexThread


@dataclass(frozen=True, slots=True)
class CodexRuntimeAdapter:
    """Codex launch/protocol/projection behavior, with no sandbox lifecycle state."""

    client_factory: CodexClientFactory = app_server_over_websocket

    @property
    def kind(self) -> RuntimeKind:
        return RuntimeKind.CODEX_APP_SERVER

    @property
    def display_name(self) -> str:
        return "Codex"

    def build_launch(self, launch: RuntimeLaunch) -> HarnessLaunch:
        """Expose the exact process launch for tests and runner diagnostics."""
        return self._native(launch).harness

    def client(
        self, websocket: TextWebSocket, launch: RuntimeLaunch, progress: ProgressSink | None, frames_to: FrameSink
    ) -> RuntimeClient:
        native = self._native(launch)
        return self.client_factory(websocket, native.harness, progress, frames_to, native.thread)

    def turn_handler(self, seed: TurnProjectionSeed = EMPTY_TURN_PROJECTION_SEED) -> RuntimeTurnHandler:
        return CodexTurnHandler(
            state=projection.ProjectionState(
                open_message=_open_item(seed.open_message),
                open_reasoning=_open_item(seed.open_reasoning),
                seen_call_ids=seed.seen_call_ids,
                completed_call_ids=seed.completed_call_ids,
            )
        )

    def wake_watcher(self) -> None:
        # Codex's idle-time frames are unclassified, so the stream is only consumed inside a turn.
        return None

    def project_log(self, frames_: Iterable[tuple[int, HarnessFrame]]) -> Projection:
        return projection.project_log(
            projection.RecordedFrame(frame_seq=seq, payload=envelope.frame) for seq, envelope in frames_
        )

    def prompt_submitted(self, outbound: Iterable[HarnessFrame]) -> bool:
        return any(frames.is_prompt(envelope.frame) for envelope in outbound)

    def _native(self, launch: RuntimeLaunch) -> _NativeLaunch:
        native_servers = {
            name: HttpMcpServer(url=server.url, bearer_token_env_var=server.bearer_environment_variable)
            for name, server in launch.mcp_servers.items()
        }
        return _NativeLaunch(
            harness=build_codex_launch(
                CodexAppServerSession(cwd=Path(launch.cwd), environment=launch.environment, mcp_servers=native_servers),
                resume_from=launch.resume_from,
            ),
            thread=CodexThread(cwd=launch.cwd, developer_instructions=launch.appended_system_prompt),
        )


@dataclass(slots=True)
class CodexTurnHandler:
    """Codex's stateful fold for one live or replayed turn."""

    state: projection.ProjectionState

    def apply(self, *, frame_seq: int, frame: HarnessFrame) -> FrameEffects:
        self.state, result = projection.project(
            self.state, [projection.RecordedFrame(frame_seq=frame_seq, payload=frame.frame)]
        )
        terminal = next((event for event in result.events if isinstance(event, TurnCompleted)), None)
        return FrameEffects(events=result.events, completion=None if terminal is None else _completion(frame, terminal))


def _open_item(seed: OpenItemSeed | None) -> projection.OpenItem | None:
    if seed is None:
        return None
    return projection.OpenItem(
        opened_at_frame_seq=seed.first_frame_seq,
        last_frame_seq=seed.last_frame_seq,
        backend_item_id=None,
        delivered=seed.text,
    )


def _completion(frame: HarnessFrame, terminal: TurnCompleted) -> TurnCompletion:
    turn = frames.terminal_turn(frame.frame)
    if turn is None:
        raise ValueError("Codex terminal event did not come from turn/completed")
    if terminal.outcome is not TurnOutcome.FAILED:
        return TurnCompletion(outcome=terminal.outcome, final_text="")
    error = turn.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        detail = message if isinstance(message, str) else str(error)
    elif error is None:
        detail = "unknown error"
    else:
        detail = str(error)
    return TurnCompletion(outcome=terminal.outcome, final_text="", failure=f"the agent's turn failed: {detail}")
