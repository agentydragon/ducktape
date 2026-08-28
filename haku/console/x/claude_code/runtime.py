"""Claude Code's provider-specific Console runtime adapter.

Sandbox claims, runner bootstrap, bridge credentials, MCP credentials and attached-chat prompt
selection are Haku infrastructure owned by ``runtime.py`` / ``session_runtime.py``.  This adapter
only translates generic launch facts into Claude argv, speaks Claude's native protocol through the
runner, and projects Claude frames into the neutral conversation vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from haku.console.harnesses.kind import HarnessKind
from haku.console.x.claude_code import frames, projection
from haku.console.x.claude_code.client import cli_over_websocket
from haku.console.x.claude_code.wake import ClaudeWakeWatcher
from haku.console.x.conversation_events import TurnCompleted, TurnFailed
from haku.console.x.runtime import (
    EMPTY_TURN_PROJECTION_SEED,
    Checkpoint,
    FrameEffects,
    RuntimeClient,
    RuntimeClientFactory,
    RuntimeLaunch,
    RuntimeTurnHandler,
    TurnCompletion,
    TurnProjectionSeed,
)
from haku.runner.claude.options import ClaudeSession, HttpMcpServer, build_claude_launch
from haku.runner.client import FrameSink
from haku.runner.protocol import HarnessFrame, HarnessLaunch, TextWebSocket
from haku.runner.transport import ProgressSink


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeAdapter:
    """Claude launch/protocol/projection behavior, with no sandbox lifecycle state."""

    client_factory: RuntimeClientFactory = cli_over_websocket

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.CLAUDE_CODE

    @property
    def display_name(self) -> str:
        return "Claude"

    def build_launch(self, launch: RuntimeLaunch) -> HarnessLaunch:
        session = ClaudeSession(
            append_system_prompt=launch.appended_system_prompt,
            cwd=Path(launch.cwd),
            environment=launch.environment,
            mcp_servers={
                name: HttpMcpServer(
                    url=server.url, headers={"Authorization": f"Bearer ${{{server.bearer_environment_variable}}}"}
                )
                for name, server in launch.mcp_servers.items()
            },
        )
        return build_claude_launch(session, resume_from=launch.resume_from)

    def client(
        self, websocket: TextWebSocket, launch: RuntimeLaunch, progress: ProgressSink | None, frames_to: FrameSink
    ) -> RuntimeClient:
        return self.client_factory(websocket, self.build_launch(launch), progress, frames_to)

    def turn_handler(self, seed: TurnProjectionSeed = EMPTY_TURN_PROJECTION_SEED) -> RuntimeTurnHandler:
        return ClaudeTurnHandler(
            state=projection.ProjectionState(
                open_message=(
                    None
                    if seed.open_message is None
                    else projection.OpenItem(
                        opened_at_frame_seq=seed.open_message.first_frame_seq,
                        last_frame_seq=seed.open_message.last_frame_seq,
                        backend_item_id=None,
                        delivered=seed.open_message.text,
                    )
                ),
                open_reasoning=(
                    None
                    if seed.open_reasoning is None
                    else projection.OpenItem(
                        opened_at_frame_seq=seed.open_reasoning.first_frame_seq,
                        last_frame_seq=seed.open_reasoning.last_frame_seq,
                        backend_item_id=None,
                        delivered=seed.open_reasoning.text,
                    )
                ),
                seen_call_ids=seed.seen_call_ids,
            )
        )

    def wake_watcher(self) -> ClaudeWakeWatcher:
        return ClaudeWakeWatcher()

    def prompt_submitted(self, outbound: Iterable[HarnessFrame]) -> bool:
        return any(frames.frame_kind(frame.frame) == frames.PROMPT_FRAME_KIND for frame in outbound)


@dataclass(slots=True)
class ClaudeTurnHandler:
    """Claude's stateful fold for one live or replayed turn."""

    state: projection.ProjectionState

    def apply(self, *, frame_seq: int, frame: HarnessFrame) -> FrameEffects:
        self.state, result = self.state.advance(
            [projection.RecordedFrame(frame_seq=frame_seq, payload=frame.frame)],
            delta_source=projection.DeltaSource.STREAM_EVENTS,
        )
        terminal = next((event for event in result.events if isinstance(event, TurnCompleted)), None)
        completion = None if terminal is None else _completion(frame, terminal)
        return FrameEffects(
            events=result.events,
            completion=completion,
            checkpoint=Checkpoint.HOLD if self.state.open_tool_call is not None else Checkpoint.ADVANCE,
        )


def _completion(frame: HarnessFrame, terminal: TurnCompleted) -> TurnCompletion:
    """The projection read the result's `subtype` and `stop_reason`; only the answer's own prose is
    still on the frame."""
    if isinstance(terminal.end, TurnFailed):
        return TurnCompletion(end=terminal.end, final_text="")
    result = frames.ResultFrame.model_validate(frame.frame)
    return TurnCompletion(end=terminal.end, final_text=str(result.result or "").strip())
