"""Read-only polling operations for the Session Dispatcher API."""

from collections.abc import Callable

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.session_dispatcher.dispatcher.api.session import (
    session_dispatcher_pb2 as pb,
    session_dispatcher_pb2_grpc,
)

BOUND_SECONDS = 300
type Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub


async def session_until(stub: Dispatcher, session: str, predicate: Callable[[pb.Session], bool]) -> pb.Session:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(BOUND_SECONDS), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            response = await stub.GetSession(pb.GetSessionRequest(session_id=session), timeout=15)
            assert predicate(response.session), response.session
            return response.session
    raise AssertionError("Session did not reach the expected state")
