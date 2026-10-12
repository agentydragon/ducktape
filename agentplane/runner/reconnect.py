"""Liveness and reconnect backoff shared by the runner's outbound streams: the control stream to the
Runner Gateway and each Session's push to the ingress."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Set
from dataclasses import dataclass

import grpc
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, wait_random_exponential

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamTiming:
    heartbeat_s: float = 10
    liveness_s: float = 30
    backoff_initial_s: float = 0.5
    backoff_max_s: float = 30


class StreamLostError(Exception):
    """A call ended before it made progress; the next one waits out the backoff."""

    def __init__(self, code: grpc.StatusCode | None, detail: str) -> None:
        super().__init__(f"{code=} {detail}")
        self.code = code


async def reconnect(
    attempt: Callable[[], Awaitable[bool]], *, timing: StreamTiming, operator_codes: Set[grpc.StatusCode], describe: str
) -> None:
    """Run `attempt` until it returns True.

    An attempt that made progress returns False and resets the backoff; one that did not raises
    `StreamLostError`, so a peer that refuses straight away cannot spin. A loss with one of
    `operator_codes`, which only an operator resolves, backs off at the maximum and logs as an error.
    """

    def wait(state: RetryCallState) -> float:
        if _lost_code(state) in operator_codes:
            return timing.backoff_max_s
        return wait_random_exponential(multiplier=timing.backoff_initial_s, max=timing.backoff_max_s)(state)

    def log_lost(state: RetryCallState) -> None:
        level = logging.ERROR if _lost_code(state) in operator_codes else logging.WARNING
        logger.log(level, "%s lost: %s", describe, state.outcome.exception() if state.outcome is not None else None)

    while True:
        async for retrying in AsyncRetrying(
            retry=retry_if_exception_type(StreamLostError), wait=wait, before_sleep=log_lost
        ):
            with retrying:
                if await attempt():
                    return


def _lost_code(state: RetryCallState) -> grpc.StatusCode | None:
    error = state.outcome.exception() if state.outcome is not None else None
    return error.code if isinstance(error, StreamLostError) else None
