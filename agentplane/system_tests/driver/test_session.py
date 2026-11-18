"""Offline safety regressions for the native Session driver."""

import asyncio
from typing import Any
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import grpc
import pytest

from agentplane.grpc.calls import ServiceError
from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, session_pb2
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver import session as session_module
from agentplane.system_tests.driver.session import Session

# gazelle:include_dep @pypi//protobuf


def _clients() -> tuple[Any, Any]:
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    channel = create_autospec(grpc.aio.Channel, instance=True)
    dispatcher.stub = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
    history = create_autospec(HistoryServiceClient, instance=True)
    return dispatcher, history


def _spec(name: str = "sandbox", uid: str = "sandbox-uid") -> session_pb2.SessionSpec:
    spec = session_pb2.SessionSpec()
    spec.placement.environment_hosted.environment.CopyFrom(session_pb2.ExactEnvironment(name=name, uid=uid))
    spec.placement.environment_hosted.harness = session_pb2.HARNESS_CODEX
    return spec


def _accepted() -> session_dispatcher_pb2.GetSessionResponse:
    return session_dispatcher_pb2.GetSessionResponse(
        session=session_dispatcher_pb2.Session(
            status=session_dispatcher_pb2.SessionStatus(
                materialization=session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED
            )
        )
    )


def _session(
    dispatcher: SessionDispatcherClient,
    history: HistoryServiceClient,
    *,
    session_id: UUID | None = None,
    cursor: int = 0,
) -> Session:
    return Session(
        dispatcher,
        history,
        session_id=session_id or uuid4(),
        environment=session_pb2.ExactEnvironment(name="sandbox", uid="sandbox-uid"),
        cursor=cursor,
    )


def _entry(cursor: int, event: event_pb2.Event) -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(cursor=cursor, event=event)


def _page(*entries: event_log_pb2.EventEntry) -> history_service_pb2.ReadSessionEventsResponse:
    page = history_service_pb2.ReadSessionEventsResponse(last_cursor=entries[-1].cursor if entries else 0)
    page.entries.extend(entries)
    return page


def _completed_turn_page(command_id: str, after_cursor: int) -> history_service_pb2.ReadSessionEventsResponse:
    command = command_pb2.Command(command_id=command_id)
    return _page(
        _entry(after_cursor + 1, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))),
        _entry(
            after_cursor + 2,
            event_pb2.Event(
                turn_completed=event_pb2.TurnCompleted(
                    turn_id=f"turn-{command_id}", status=event_pb2.TURN_STATUS_COMPLETED
                )
            ),
        ),
    )


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.mark.parametrize("failure_kind", ["unavailable", "deadline"])
async def test_create_session_does_not_retry_an_uncertain_mutation(failure_kind: str) -> None:
    dispatcher, history = _clients()
    session_id = uuid4()
    spec = _spec()
    failure: BaseException = (
        ServiceError(grpc.StatusCode.UNAVAILABLE, "create reply lost")
        if failure_kind == "unavailable"
        else TimeoutError("create deadline expired")
    )
    dispatcher.unary.side_effect = failure

    with pytest.raises(type(failure)):
        await Session.create(dispatcher, history, spec=spec, session_id=session_id, setup_script="printf setup")

    dispatcher.unary.assert_awaited_once()
    create_call, request = dispatcher.unary.call_args.args
    assert create_call is dispatcher.stub.CreateSession
    assert request == session_dispatcher_pb2.CreateSessionRequest(
        session_id=str(session_id), spec=spec, setup_script="printf setup"
    )
    history.read_session_events.assert_not_awaited()
    dispatcher.submit_input.assert_not_awaited()


@pytest.mark.parametrize("failure_kind", ["unavailable", "deadline"])
async def test_submit_input_does_not_replay_an_uncertain_mutation(failure_kind: str) -> None:
    dispatcher, history = _clients()
    session = _session(dispatcher, history)
    failure: BaseException = (
        ServiceError(grpc.StatusCode.UNAVAILABLE, "input reply lost")
        if failure_kind == "unavailable"
        else TimeoutError("input deadline expired")
    )
    dispatcher.submit_input.side_effect = failure

    with pytest.raises(type(failure)):
        await session.submit_input("known-command-id", "keep this exact input")

    dispatcher.submit_input.assert_awaited_once_with(
        str(session.session_id), "known-command-id", "sandbox", "sandbox-uid", "keep this exact input"
    )
    history.read_session_events.assert_not_awaited()


async def test_created_session_keeps_the_original_environment_identity() -> None:
    dispatcher, history = _clients()
    session_id = uuid4()
    spec = _spec()
    dispatcher.unary.side_effect = [session_dispatcher_pb2.CreateSessionResponse(), _accepted()]

    session = await Session.create(dispatcher, history, spec=spec, session_id=session_id)
    create_call, create_request = dispatcher.unary.call_args_list[0].args
    get_call, get_request = dispatcher.unary.call_args_list[1].args
    assert create_call is dispatcher.stub.CreateSession
    assert create_request == session_dispatcher_pb2.CreateSessionRequest(session_id=str(session_id), spec=spec)
    assert get_call is dispatcher.stub.GetSession
    assert get_request == session_dispatcher_pb2.GetSessionRequest(session_id=str(session_id))

    spec.placement.environment_hosted.environment.name = "replacement"
    spec.placement.environment_hosted.environment.uid = "replacement-uid"
    await session.submit_input("fixed-command-id", "use the created Session")

    dispatcher.submit_input.assert_awaited_once_with(
        str(session_id), "fixed-command-id", "sandbox", "sandbox-uid", "use the created Session"
    )


async def test_history_retries_only_initial_read_outages_and_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatcher, history = _clients()
    session = _session(dispatcher, history)
    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(session_module.asyncio, "sleep", no_sleep)
    attempts: list[int] = []

    async def read_page(
        _session_id: str, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del limit
        attempts.append(after_cursor)
        match len(attempts):
            case 1:
                raise ServiceError(grpc.StatusCode.UNAVAILABLE, "history is restarting")
            case 2:
                raise TimeoutError("history read deadline")
            case 3:
                raise ServiceError(grpc.StatusCode.NOT_FOUND, "session not attached yet")
            case _:
                command_id = dispatcher.submit_input.call_args.args[1]
                return _completed_turn_page(command_id, after_cursor)

    history.read_session_events.side_effect = read_page
    turn = await session.run("one explicit prompt")

    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    assert turn.input_confirmed is False
    assert attempts == [0, 0, 0, 0]
    assert len(sleeps) == 3
    assert dispatcher.submit_input.await_count == 1


@pytest.mark.parametrize("failure_kind", ["not_found", "permission_denied", "invalid_page"])
async def test_history_errors_after_attachment_fail_without_retry(
    failure_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, history = _clients()
    session = _session(dispatcher, history)
    monkeypatch.setattr(session_module.asyncio, "sleep", _no_sleep)
    empty = history_service_pb2.ReadSessionEventsResponse()
    failure: ServiceError | history_service_pb2.ReadSessionEventsResponse
    expected: type[Exception]
    if failure_kind == "not_found":
        failure = ServiceError(grpc.StatusCode.NOT_FOUND, "attached Session disappeared")
        history.read_session_events.side_effect = [empty, failure]
        expected = ServiceError
    elif failure_kind == "permission_denied":
        failure = ServiceError(grpc.StatusCode.PERMISSION_DENIED, "History authorization refused")
        history.read_session_events.side_effect = [empty, failure]
        expected = ServiceError
    else:
        failure = _page(_entry(2, event_pb2.Event()))
        history.read_session_events.side_effect = [empty, failure]
        expected = ValueError

    with pytest.raises(expected):
        await session.next_turn()

    assert history.read_session_events.await_count == 2


async def test_history_attachment_remains_sticky_across_turn_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatcher, history = _clients()
    session = _session(dispatcher, history)
    monkeypatch.setattr(session_module, "TURN_SECONDS", 0.01)
    history.read_session_events.side_effect = [
        history_service_pb2.ReadSessionEventsResponse(),
        ServiceError(grpc.StatusCode.NOT_FOUND, "attached Session disappeared"),
    ]

    with pytest.raises(TimeoutError):
        await session.next_turn()
    with pytest.raises(ServiceError, match="attached Session disappeared"):
        await session.next_turn()

    assert history.read_session_events.await_count == 2


async def test_failed_terminal_advances_cursor_for_a_later_explicit_input() -> None:
    dispatcher, history = _clients()
    session = _session(dispatcher, history, cursor=4)
    attempts: list[int] = []

    async def read_page(
        _session_id: str, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del limit
        attempts.append(after_cursor)
        command_id = dispatcher.submit_input.call_args.args[1]
        if dispatcher.submit_input.await_count == 1:
            return _page(
                _entry(
                    after_cursor + 1,
                    event_pb2.Event(
                        command_admitted=event_pb2.CommandAdmitted(command=command_pb2.Command(command_id=command_id))
                    ),
                ),
                _entry(
                    after_cursor + 2,
                    event_pb2.Event(
                        turn_completed=event_pb2.TurnCompleted(
                            turn_id="failed-turn", status=event_pb2.TURN_STATUS_FAILED, error="setup error"
                        )
                    ),
                ),
            )
        return _completed_turn_page(command_id, after_cursor)

    history.read_session_events.side_effect = read_page

    await session.submit_input("failed-command-id", "first input failed")
    with pytest.raises(AssertionError, match="setup error"):
        await session.next_turn(awaiting_command_id="failed-command-id")
    assert session._cursor == 6

    await session.submit_input("next-command-id", "later explicit input")
    turn = await session.next_turn(awaiting_command_id="next-command-id")

    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    assert attempts == [4, 6]
    assert dispatcher.submit_input.await_count == 2
    first, second = dispatcher.submit_input.call_args_list
    assert first.args[4] == "first input failed"
    assert second.args[4] == "later explicit input"
    assert first.args[1] == "failed-command-id"
    assert second.args[1] == "next-command-id"


async def test_next_turn_deadline_bounds_quiet_history(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatcher, history = _clients()
    session = _session(dispatcher, history)
    monkeypatch.setattr(session_module, "TURN_SECONDS", 0.01)
    history.read_session_events.return_value = history_service_pb2.ReadSessionEventsResponse()

    with pytest.raises(TimeoutError):
        await session.next_turn()

    history.read_session_events.assert_awaited_once()


async def test_cancelling_a_waiting_turn_does_not_replay_its_input() -> None:
    dispatcher, history = _clients()
    session = _session(dispatcher, history)
    read_started = asyncio.Event()
    read_cancelled = asyncio.Event()

    async def blocked_read(
        _session_id: str, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del after_cursor, limit
        read_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            read_cancelled.set()
        raise AssertionError("blocked History read returned without cancellation")

    history.read_session_events.side_effect = blocked_read
    async with asyncio.timeout(1), asyncio.TaskGroup() as tasks:
        task = tasks.create_task(session.run("cancelled input"))
        await read_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert read_cancelled.is_set()
    dispatcher.submit_input.assert_awaited_once()
    history.read_session_events.assert_awaited_once()


async def test_interrupted_setup_is_a_failure_and_consumes_its_terminal() -> None:
    dispatcher, history = _clients()
    session = _session(dispatcher, history)
    history.read_session_events.return_value = _page(
        _entry(1, event_pb2.Event(setup_output=event_pb2.SetupOutput(stdout=b"partial output\n"))),
        _entry(2, event_pb2.Event(setup_output=event_pb2.SetupOutput(stderr=b"permission denied\n"))),
        _entry(3, event_pb2.Event(setup_interrupted=event_pb2.SetupInterrupted())),
    )

    with pytest.raises(AssertionError, match="interrupted") as caught:
        await session.setup_output()

    assert "permission denied" in str(caught.value)
    assert session._cursor == 3
    history.read_session_events.assert_awaited_once()
