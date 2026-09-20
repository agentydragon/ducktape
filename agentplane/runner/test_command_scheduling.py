"""Runner command admission remains responsive while a native operation is blocked."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest_bazel

from agentplane.protocol import command_pb2, event_pb2
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.config import RunnerConfig
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.journal import Journal
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord, SessionStore, StateOwner

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class BlockingAdapter(HarnessAdapter):
    def __init__(self) -> None:
        self.submit_started = asyncio.Event()
        self.release_submit = asyncio.Event()
        self.submit_finished = asyncio.Event()
        self.interrupt_started = asyncio.Event()
        self.interrupted_turn_ids: list[str] = []

    def command(self) -> list[str]:
        return []

    def environment(self) -> Mapping[str, str]:
        return {}

    async def handshake(self) -> str:
        return "native-session"

    async def submit(self, command_id: str, text: str) -> None:
        assert (command_id, text) == ("input-1", "blocked native input")
        self.submit_started.set()
        await self.release_submit.wait()
        self.submit_finished.set()

    async def interrupt(self, turn_id: str) -> None:
        self.interrupted_turn_ids.append(turn_id)
        self.interrupt_started.set()

    async def change_model(self, command_id: str, model: str) -> None:
        raise AssertionError(f"unexpected model command {(command_id, model)!r}")

    async def on_frame(self, frame: dict[str, Any], source_sequence: int) -> None:
        raise AssertionError(f"unexpected native frame {(frame, source_sequence)!r}")


class RunningProcess:
    running = True


@asynccontextmanager
async def session_with_blocked_adapter(tmp_path: Path) -> AsyncIterator[tuple[Session, BlockingAdapter]]:
    state_dir = tmp_path / "state"
    store = SessionStore(state_dir / "sessions")
    owner = StateOwner(state_dir)
    record = SessionRecord(
        harness="HARNESS_CODEX", cwd=str(tmp_path / "workspace"), model="test-model", reasoning_effort="low"
    )
    store.write("scheduling-1", record)
    adapter = BlockingAdapter()
    try:
        async with Journal.open(
            store.directory("scheduling-1") / "journal.sqlite", str(record.event_source_id)
        ) as journal:
            session = Session(
                "scheduling-1",
                record=record,
                journal=journal,
                store=store,
                config=RunnerConfig(state_dir=state_dir),
                make_adapter=lambda _session: adapter,
                state_owner_descriptor=owner.descriptor,
            )
            session.adapter = adapter
            session.process = cast(HarnessProcess, RunningProcess())
            session.active_turn_id = "turn-1"
            yield session, adapter
    finally:
        owner.close()


async def test_interrupt_is_admitted_and_dispatched_while_a_prior_input_blocks(tmp_path: Path) -> None:
    async with session_with_blocked_adapter(tmp_path) as (session, adapter):
        input_task = asyncio.create_task(
            session.command(
                command_pb2.Command(
                    command_id="input-1", submit_input=command_pb2.SubmitInput(text="blocked native input")
                )
            )
        )
        interrupt_task: asyncio.Task[None] | None = None
        try:
            await adapter.submit_started.wait()
            assert input_task.done()
            await input_task
            interrupt_task = asyncio.create_task(
                session.command(
                    command_pb2.Command(
                        command_id="interrupt-1", interrupt_turn=command_pb2.InterruptTurn(turn_id="turn-1")
                    )
                )
            )
            await adapter.interrupt_started.wait()
            assert interrupt_task.done()
            await interrupt_task

            assert adapter.interrupted_turn_ids == ["turn-1"]
            assert [
                entry.event.command_admitted.command.command_id
                for entry in session.journal.entries
                if entry.event.HasField("command_admitted")
            ] == ["input-1", "interrupt-1"]
            input_entry = await session.journal.get("input-1")
            interrupt_entry = await session.journal.get("interrupt-1")
            assert input_entry is not None
            assert input_entry.dispatch_planned
            assert interrupt_entry is not None
            assert interrupt_entry.dispatch_planned

            await session.turn_completed("turn-1", event_pb2.TURN_STATUS_INTERRUPTED)
            interrupt_entry = await session.journal.get("interrupt-1")
            assert interrupt_entry is not None
            assert interrupt_entry.terminal_cursor is not None
        finally:
            adapter.release_submit.set()
            await adapter.submit_finished.wait()
            normal_dispatch = session._normal_dispatch_task
            if normal_dispatch is not None:
                await normal_dispatch
            await asyncio.gather(
                input_task, *(task for task in [interrupt_task] if task is not None), return_exceptions=True
            )


if __name__ == "__main__":
    pytest_bazel.main()
