"""Runner command admission remains responsive while a native operation is blocked."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_bazel

from agentplane.protocol import command_pb2, event_pb2
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.config import CodexLaunch, RunnerConfig
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.journal import Journal
from agentplane.runner.model_config import HttpModelConfigResolver
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
        self.model_changes: list[tuple[str, str]] = []

    def command(self) -> list[str]:
        return []

    def environment(self) -> Mapping[str, str]:
        return {}

    async def handshake(self) -> str:
        return "native-session"

    async def reconcile(self, turn_id: str, *, resumed: bool) -> event_pb2.ConversationReconciled:
        return event_pb2.ConversationReconciled(turn_id=turn_id)

    async def submit(self, command_id: str, text: str) -> None:
        assert (command_id, text) == ("input-1", "blocked native input")
        self.submit_started.set()
        await self.release_submit.wait()
        self.submit_finished.set()

    async def interrupt(self, turn_id: str) -> None:
        self.interrupted_turn_ids.append(turn_id)
        self.interrupt_started.set()

    async def change_model(self, command_id: str, model: str) -> None:
        self.model_changes.append((command_id, model))

    async def change_reasoning_effort(self, command_id: str, effort: str) -> None:
        raise AssertionError(f"unexpected effort command {(command_id, effort)!r}")

    async def on_frame(self, frame: dict[str, Any], source_sequence: int) -> None:
        raise AssertionError(f"unexpected native frame {(frame, source_sequence)!r}")


class RunningProcess:
    running = True


def model_config_resolver(windows: Mapping[str, int]) -> HttpModelConfigResolver:
    def respond(request: httpx.Request) -> httpx.Response:
        model = request.url.params["model"]
        if window := windows.get(model):
            return httpx.Response(200, json={"model": model, "total_context_budget_tokens": window})
        return httpx.Response(404, json={"detail": "no configuration for model"})

    return HttpModelConfigResolver(transport=httpx.MockTransport(respond))


@asynccontextmanager
async def session_with_blocked_adapter(
    tmp_path: Path, *, model_configs: Mapping[str, int] | None = None, initial_model: str = "test-model"
) -> AsyncIterator[tuple[Session, BlockingAdapter]]:
    state_dir = tmp_path / "state"
    store = SessionStore(state_dir / "sessions")
    owner = StateOwner(state_dir)
    windows = model_configs or {}
    record = SessionRecord(
        harness="HARNESS_CODEX",
        cwd=str(tmp_path / "workspace"),
        model=initial_model,
        total_context_budget_tokens=windows.get(initial_model),
        context_budget_resolved=True,
        reasoning_effort="low",
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
                config=RunnerConfig(
                    state_dir=state_dir,
                    model_config_resolver=model_config_resolver(windows),
                    codex=CodexLaunch(binary=Path("/bin/true"), base_url="http://ingress/v1", api_key="test"),
                ),
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
                for entry in (await session.journal.since(0, limit=128))
                if entry.event.HasField("command_admitted")
            ] == ["input-1", "interrupt-1"]
            input_entry = await session.journal.get("input-1")
            interrupt_entry = await session.journal.get("interrupt-1")
            assert input_entry is not None
            assert input_entry.dispatch_planned
            assert interrupt_entry is not None
            assert interrupt_entry.dispatch_planned

            await session.turn_completed("turn-1", event_pb2.TURN_STATUS_INTERRUPTED, sources=[])
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


async def test_terminal_commands_release_scheduling_state_and_retry_is_deduplicated(tmp_path: Path) -> None:
    async with session_with_blocked_adapter(tmp_path) as (session, _):
        commands = [
            command_pb2.Command(command_id=f"model-{i}", change_model=command_pb2.ChangeModel(model="test-model"))
            for i in range(32)
        ]
        for command in commands:
            await session.command(command)
            dispatch = session._normal_dispatch_task
            if dispatch is not None:
                await dispatch
            stored = await session.journal.get(command.command_id)
            assert stored is not None
            assert stored.terminal_cursor is not None
        assert not session._scheduled_commands
        assert not session._dispatched_commands
        cursor = session.journal.last_cursor
        for command in commands:
            await session.command(command)
        assert session.journal.last_cursor == cursor
        assert not session._scheduled_commands
        assert session._normal_dispatch_task is None


@pytest.mark.parametrize(
    ("initial_model", "requested_model", "context_windows"),
    [
        ("qwen-128", "qwen-256", {"qwen-128": 128 * 1024, "qwen-256": 256 * 1024}),
        ("qwen-128", "unlisted", {"qwen-128": 128 * 1024}),
        ("unlisted", "qwen-128", {"qwen-128": 128 * 1024}),
    ],
)
async def test_a_model_change_with_a_different_or_missing_context_window_fails(
    tmp_path: Path, initial_model: str, requested_model: str, context_windows: dict[str, int]
) -> None:
    async with session_with_blocked_adapter(tmp_path, initial_model=initial_model, model_configs=context_windows) as (
        session,
        adapter,
    ):
        command = command_pb2.Command(
            command_id="different-window", change_model=command_pb2.ChangeModel(model=requested_model)
        )
        await session.command(command)
        dispatch = session._normal_dispatch_task
        if dispatch is not None:
            await dispatch

        result = next(
            entry.event
            for entry in await session.journal.since(0, limit=16)
            if entry.event.WhichOneof("observation") == "command_failed"
        )
        assert "requires a new thread" in result.command_failed.reason
        assert "new session" in result.command_failed.reason
        assert adapter.model_changes == []


async def test_a_same_window_model_change_reaches_the_harness(tmp_path: Path) -> None:
    async with session_with_blocked_adapter(
        tmp_path, initial_model="qwen-128", model_configs={"qwen-128": 128 * 1024, "qwen-128-alias": 128 * 1024}
    ) as (session, adapter):
        await session.command(
            command_pb2.Command(command_id="same-window", change_model=command_pb2.ChangeModel(model="qwen-128-alias"))
        )
        dispatch = session._normal_dispatch_task
        if dispatch is not None:
            await dispatch
        assert adapter.model_changes == [("same-window", "qwen-128-alias")]


if __name__ == "__main__":
    pytest_bazel.main()
