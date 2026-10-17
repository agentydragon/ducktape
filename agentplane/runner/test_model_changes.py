"""Model switches retain the conversation while updating startup-only context budgets."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import httpx
import pytest
import pytest_bazel

from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.config import RunnerConfig
from agentplane.runner.model_config import HttpModelConfigResolver
from agentplane.runner.runner import Runner
from agentplane.runner.testing import events, launches
from agentplane.runner.testing.driver import RunnerDriver
from agentplane.runner.testing.fixtures import ModelEndpoint
from agentplane.runner.testing.scripted_model import ScriptedModel, Text

# The generated protobuf stub chain needs a direct dependency for the mypy aspect.
# gazelle:include_dep @pypi//protobuf


@pytest.fixture(params=[(131072, 262144), (None, 131072), (131072, None)])
def windows(request: pytest.FixtureRequest) -> tuple[int | None, int | None]:
    return cast(tuple[int | None, int | None], request.param)


@pytest.fixture
def selected(harness: runner_pb2.Harness) -> str:
    return "agentplane-switched/claude-haiku-4-5-20251001" if harness == runner_pb2.HARNESS_CLAUDE else "switched-model"


@pytest.fixture
def config(
    harness: runner_pb2.Harness,
    endpoint: ModelEndpoint,
    tmp_path: Path,
    windows: tuple[int | None, int | None],
    selected: str,
) -> RunnerConfig:
    def respond(request: httpx.Request) -> httpx.Response:
        model = request.url.params["model"]
        window = windows[1] if model == selected else windows[0]
        if window is None:
            return httpx.Response(200, content=b"null")
        return httpx.Response(200, json={"model": model, "total_context_budget_tokens": window})

    return replace(
        launches.config(harness, endpoint.origin, state_dir=tmp_path / "state", home=tmp_path / "home"),
        model_config_resolver=HttpModelConfigResolver(transport=httpx.MockTransport(respond)),
    )


@pytest.mark.parametrize(
    "prior_turn", ["none", "completed", "active"], ids=["before-first-input", "after-turn", "during-turn"]
)
async def test_switch_model_and_context_budget_in_existing_conversation(
    driver: RunnerDriver,
    runner: Runner,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    windows: tuple[int | None, int | None],
    selected: str,
    prior_turn: str,
) -> None:
    session = await driver.attach("model-budget", spec=spec)
    retained = runner.sessions[session.attached.session_id]
    native_id = retained.record.native_session_id
    if prior_turn != "none":
        await session.submit_input("seed", "Remember the word PINEAPPLE.")
        request = await asyncio.wait_for(model.request(), timeout=20)
        if prior_turn == "completed":
            await model.reply(request, Text("Remembered PINEAPPLE."))
            await session.until(events.turn_completed)

    await session.change_model("switch", selected)
    await session.submit_input("input", "Reply with SWITCHED_OK.")
    if prior_turn == "active":
        await model.reply(request, Text("Remembered PINEAPPLE."))
        completed = await session.until(events.turn_completed)
        assert completed.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    request = await asyncio.wait_for(model.request(), timeout=20)
    assert request.model == selected
    if prior_turn != "none":
        assert "Remember the word PINEAPPLE." in request.user_texts
    await model.reply(request, Text("SWITCHED_OK"))
    changed = await session.until(events.is_kind("model_changed"))
    assert changed.event.model_changed.command_id == "switch"
    assert changed.event.model_changed.model == selected
    await session.until(events.turn_completed)
    if prior_turn != "none" or spec.harness == runner_pb2.HARNESS_CLAUDE:
        assert retained.record.native_session_id == native_id
    assert session.attached.session_id == retained.session_id
    assert retained.record.model == selected
    assert retained.record.total_context_budget_tokens == windows[1]
    persisted = retained.store.read(session.attached.session_id)
    assert persisted.model == selected
    assert persisted.total_context_budget_tokens == windows[1]
    assert not events.of_kind(session.seen, "command_failed")
    assert any(entry.event.harness_exited.restarting for entry in events.of_kind(session.seen, "harness_exited"))

    # Native usage proves the process consumed the changed setting, rather than only persisting it.
    if spec.harness == runner_pb2.HARNESS_CODEX:
        expected_window = (windows[1] or 272000) * 95 // 100
        updates = [json.loads(entry.event.native.line) for entry in events.of_kind(session.seen, "native")]
        usage = [update for update in updates if update.get("method") == "thread/tokenUsage/updated"]
        assert usage
        assert usage[-1]["params"]["tokenUsage"]["modelContextWindow"] == expected_window


@pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CODEX])
async def test_selecting_original_model_supersedes_pending_codex_selection(
    driver: RunnerDriver,
    runner: Runner,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    selected: str,
    windows: tuple[int | None, int | None],
) -> None:
    session = await driver.attach("revert-model", spec=spec)
    await session.change_model("switch-away", selected)
    await session.change_model("switch-back", spec.model)
    await session.submit_input("input", "Keep the original model.")
    request = await asyncio.wait_for(model.request(), timeout=20)
    assert request.model == spec.model
    await model.reply(request, Text("ORIGINAL_OK"))
    changed = await session.until(events.is_kind("model_changed"))
    assert changed.event.model_changed.command_id == "switch-back"
    await session.until(events.turn_completed)
    assert [entry.event.command_noop.command_id for entry in events.of_kind(session.seen, "command_noop")] == [
        "switch-away"
    ]
    assert runner.sessions[session.attached.session_id].record.total_context_budget_tokens == windows[0]
    assert not events.of_kind(session.seen, "harness_exited")


if __name__ == "__main__":
    pytest_bazel.main()
