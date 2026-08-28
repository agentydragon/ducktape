"""Codex app-server's provider-specific Console runtime adapter.

The shared Console loop supplies neutral launch facts and durable projection seeds.  This adapter
turns those into Codex's process launch, JSON-RPC thread configuration, connected client and native
frame reducer.  No Codex method or item discriminator escapes this package.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from haku.console.harnesses.kind import HarnessKind
from haku.console.x.codex_app_server import frames, projection
from haku.console.x.codex_app_server.client import CodexClientFactory, CodexThread, app_server_over_websocket
from haku.console.x.codex_app_server.config import ReasoningEffort
from haku.console.x.conversation_events import TurnCompleted
from haku.console.x.runtime import (
    EMPTY_TURN_PROJECTION_SEED,
    FrameEffects,
    OpenItemSeed,
    RuntimeClient,
    RuntimeLaunch,
    RuntimeTurnHandler,
    RuntimeUnusable,
    TurnCompletion,
    TurnProjectionSeed,
)
from haku.runtime.x.bridge.client import FrameSink
from haku.runtime.x.bridge.codex_options import (
    CodexAppServerSession,
    CodexModelProvider,
    HttpMcpServer,
    build_codex_launch,
)
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
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    model_provider: CodexModelProvider | None = None

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.CODEX_APP_SERVER

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

    def prompt_submitted(self, outbound: Iterable[HarnessFrame]) -> bool:
        return any(frames.is_prompt(envelope.frame) for envelope in outbound)

    def _native(self, launch: RuntimeLaunch) -> _NativeLaunch:
        native_servers = {
            name: HttpMcpServer(url=server.url, bearer_token_env_var=server.bearer_environment_variable)
            for name, server in launch.mcp_servers.items()
        }
        cwd = Path(launch.cwd)
        return _NativeLaunch(
            harness=build_codex_launch(
                CodexAppServerSession(
                    cwd=cwd,
                    environment=launch.environment,
                    mcp_servers=native_servers,
                    model_provider=self.model_provider,
                ),
                resume_from=launch.resume_from,
            ),
            thread=CodexThread(
                cwd=cwd,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                developer_instructions=launch.appended_system_prompt,
            ),
        )


@dataclass(slots=True)
class CodexTurnHandler:
    """Codex's stateful fold for one live or replayed turn."""

    state: projection.ProjectionState

    def apply(self, *, frame_seq: int, frame: HarnessFrame) -> FrameEffects:
        self.state, result = self.state.advance([projection.RecordedFrame(frame_seq=frame_seq, payload=frame.frame)])
        terminal = next((event for event in result.events if isinstance(event, TurnCompleted)), None)
        completion = None if terminal is None else TurnCompletion(end=terminal.end, final_text="")
        # Arrives before the terminal frame, so the loop carries it to the turn's close.
        unusable = (
            RuntimeUnusable(reason="the Codex thread reported a system error")
            if frames.system_error(frame.frame)
            else None
        )
        return FrameEffects(events=result.events, completion=completion, unusable=unusable)


def _open_item(seed: OpenItemSeed | None) -> projection.OpenItem | None:
    if seed is None:
        return None
    return projection.OpenItem(
        opened_at_frame_seq=seed.first_frame_seq,
        last_frame_seq=seed.last_frame_seq,
        backend_item_id=None,
        delivered=seed.text,
    )
