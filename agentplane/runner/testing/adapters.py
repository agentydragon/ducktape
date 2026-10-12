"""A harness adapter for tests that stand in for the harness: every native operation a test does not
override fails it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agentplane.proto import event_pb2
from agentplane.runner.adapter import HarnessAdapter

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class UnexpectedAdapter(HarnessAdapter):
    """Launches nothing, handshakes and reconciles trivially, and raises on any command or frame."""

    def command(self) -> list[str]:
        return []

    def environment(self) -> Mapping[str, str]:
        return {}

    async def handshake(self) -> str:
        return "test-native-session"

    async def reconcile(self, turn_id: str, *, resumed: bool) -> event_pb2.ConversationReconciled:
        return event_pb2.ConversationReconciled(turn_id=turn_id)

    async def submit(self, command_id: str, text: str) -> None:
        raise AssertionError(f"unexpected input {(command_id, text)!r}")

    async def interrupt(self, turn_id: str) -> None:
        raise AssertionError(f"unexpected interrupt {turn_id!r}")

    async def change_model(self, command_id: str, model: str) -> None:
        raise AssertionError(f"unexpected model command {(command_id, model)!r}")

    async def change_reasoning_effort(self, command_id: str, effort: str) -> None:
        raise AssertionError(f"unexpected effort command {(command_id, effort)!r}")

    async def on_frame(self, frame: dict[str, Any], source_sequence: int) -> None:
        raise AssertionError(f"unexpected native frame {(frame, source_sequence)!r}")
