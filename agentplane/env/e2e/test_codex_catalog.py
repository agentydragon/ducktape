"""Codex takes its context windows from the model catalog its provider names, through the runner."""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.env.e2e.testing import events, launches
from agentplane.env.e2e.testing.driver import RunnerDriver, SessionFollow
from agentplane.native.codex.testing import model_catalog
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import event_pb2, session_pb2
from agentplane.testing.scripted_model import ScriptedModel, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.parametrize("harness", [session_pb2.HARNESS_CODEX], ids=["codex"])

SMALL = 100_000
LARGE = 300_000
SWITCHED = "agentplane-switched-model"


@pytest.fixture
def codex_endpoint(endpoint: object) -> OpenAIResponses:
    assert isinstance(endpoint, OpenAIResponses)
    return endpoint


async def _turn(session: SessionFollow, model: ScriptedModel, number: int) -> int:
    """Run one turn and return the window Codex reported for it."""
    await session.submit_input(f"input-{number}", f"Reply with exactly: TURN_{number}_OK")
    request = await model.request()
    await model.reply(request, Text(f"TURN_{number}_OK"))
    done = await session.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    return events.codex_context_windows(session.seen)[-1]


async def test_context_window_comes_from_the_served_catalog_entry(
    driver: RunnerDriver, model: ScriptedModel, codex_endpoint: OpenAIResponses, spec: session_pb2.SessionSpec
) -> None:
    codex_endpoint.serve_catalog([model_catalog.entry(MODEL, context_window=SMALL)])
    session = await driver.attach("catalog-window-1", spec=spec)

    assert await _turn(session, model, 1) == model_catalog.usable_window(SMALL)
    assert set(codex_endpoint.catalog_authorizations) == {f"Bearer {launches.TOKEN}"}


async def test_catalog_edit_reaches_the_running_codex_without_a_restart(
    driver: RunnerDriver, model: ScriptedModel, codex_endpoint: OpenAIResponses, spec: session_pb2.SessionSpec
) -> None:
    codex_endpoint.serve_catalog([model_catalog.entry(MODEL, context_window=SMALL)])
    session = await driver.attach("catalog-edit-1", spec=spec)
    assert await _turn(session, model, 1) == model_catalog.usable_window(SMALL)

    codex_endpoint.serve_catalog([model_catalog.entry(MODEL, context_window=LARGE)])
    # The reply that carries the changed `X-Models-Etag` makes Codex refetch; the turn after it uses the entry.
    await _turn(session, model, 2)
    assert await _turn(session, model, 3) == model_catalog.usable_window(LARGE)
    assert len(events.of_kind(session.seen, "harness_started")) == 1


async def test_switching_between_routes_of_different_windows_follows_each_entry(
    driver: RunnerDriver, model: ScriptedModel, codex_endpoint: OpenAIResponses, spec: session_pb2.SessionSpec
) -> None:
    codex_endpoint.serve_catalog(
        [model_catalog.entry(MODEL, context_window=SMALL), model_catalog.entry(SWITCHED, context_window=LARGE)]
    )
    session = await driver.attach("catalog-switch-1", spec=spec)
    assert await _turn(session, model, 1) == model_catalog.usable_window(SMALL)

    await session.change_model("switch-1", SWITCHED)
    model.model = SWITCHED
    assert await _turn(session, model, 2) == model_catalog.usable_window(LARGE)
    (changed,) = events.of_kind(session.seen, "model_changed")
    assert changed.event.model_changed.model == SWITCHED


async def test_failing_catalog_refetch_does_not_fail_turns(
    driver: RunnerDriver, model: ScriptedModel, codex_endpoint: OpenAIResponses, spec: session_pb2.SessionSpec
) -> None:
    codex_endpoint.serve_catalog([model_catalog.entry(MODEL, context_window=SMALL)])
    session = await driver.attach("catalog-failure-1", spec=spec)
    await _turn(session, model, 1)
    fetches = len(codex_endpoint.catalog_authorizations)

    codex_endpoint.serve_catalog([model_catalog.entry(MODEL, context_window=LARGE)], status=500)
    await _turn(session, model, 2)
    await _turn(session, model, 3)
    assert len(codex_endpoint.catalog_authorizations) > fetches


if __name__ == "__main__":
    pytest_bazel.main()
