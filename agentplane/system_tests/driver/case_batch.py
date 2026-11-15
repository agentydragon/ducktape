"""Selected pytest cases sharing a deployment, with bounded case-owned async work."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

import pytest
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Status, StatusCode

from agentplane.system_tests.driver.tracing import tracer


@dataclass(frozen=True)
class Case:
    nodeid: str
    run: Callable[[], Awaitable[None]]
    exclusive: bool = False
    timeout_seconds: float | None = None

    @classmethod
    def for_item(cls, item: pytest.Function, run: Callable[[], Awaitable[None]]) -> Case:
        timeout = item.get_closest_marker("timeout")
        seconds = float(timeout.args[0] if timeout.args else timeout.kwargs["timeout"]) if timeout else None
        return cls(
            nodeid=item.nodeid,
            run=run,
            exclusive=item.get_closest_marker("exclusive_case") is not None,
            timeout_seconds=seconds or None,
        )


@dataclass(frozen=True)
class Result:
    error: BaseException | None
    seconds: float


CASE_RESULT = pytest.StashKey[Result]()


class CaseBatch:
    def __init__(self, cases: Mapping[str, Case]) -> None:
        loop = asyncio.get_running_loop()
        self._results: dict[str, asyncio.Future[Result]] = {name: loop.create_future() for name in cases}

    async def result(self, name: str) -> Result:
        return await asyncio.shield(self._results[name])

    def _finish_pending(self, error: BaseException) -> None:
        for future in self._results.values():
            if not future.done():
                future.set_result(Result(error=error, seconds=0))

    async def report(self, request: pytest.FixtureRequest) -> None:
        result = await self.result(request.node.nodeid)
        request.node.stash[CASE_RESULT] = result
        if result.error is not None:
            raise result.error

    async def _run_case(self, name: str, case: Case) -> None:
        error: BaseException | None = None
        with tracer.start_as_current_span(
            "system_test.case", context=Context(), attributes={"pytest.nodeid": case.nodeid}
        ) as span:
            try:
                with tracer.start_as_current_span("system_test.scenario", attributes={"pytest.nodeid": case.nodeid}):
                    async with asyncio.timeout(case.timeout_seconds):
                        await case.run()
            except (
                Exception,
                asyncio.CancelledError,
                pytest.fail.Exception,
                pytest.skip.Exception,
                pytest.xfail.Exception,
            ) as caught:
                # The matching pytest item re-raises this after its owned cleanup completes.
                error = caught
                span.set_status(Status(StatusCode.ERROR, str(caught)))
            except BaseExceptionGroup as caught:
                # TaskGroup preserves pytest control outcomes as BaseException leaves. Record
                # groups made only of case failures, but let cancellation and process-control
                # exceptions continue through the task group.
                _, unhandled = caught.split(
                    (Exception, pytest.fail.Exception, pytest.skip.Exception, pytest.xfail.Exception)
                )
                if unhandled is not None:
                    raise
                error = caught
                span.set_status(Status(StatusCode.ERROR, str(caught)))
        assert isinstance(span, ReadableSpan)
        assert span.start_time is not None
        assert span.end_time is not None
        self._results[name].set_result(Result(error=error, seconds=(span.end_time - span.start_time) / 1e9))

    async def _run(self, cases: Mapping[str, Case], *, limit: int) -> None:
        for name, case in cases.items():
            if not case.exclusive:
                continue
            await self._run_case(name, case)
            if (await self.result(name)).error is not None:
                self._finish_pending(RuntimeError(f"Not run: exclusive case {name} failed"))
                return
        slots = asyncio.Semaphore(limit)

        async def bounded(name: str, case: Case) -> None:
            async with slots:
                await self._run_case(name, case)

        async with asyncio.TaskGroup() as tasks:
            for name, case in cases.items():
                if not case.exclusive:
                    tasks.create_task(bounded(name, case), name=name)

    @classmethod
    @asynccontextmanager
    async def running(cls, cases: Mapping[str, Case], *, limit: int) -> AsyncIterator[CaseBatch]:
        if limit < 1:
            raise ValueError("case concurrency must be positive")
        batch = cls(cases)

        # The driver owns its TaskGroup entirely within one task; pytest async fixture
        # setup and finalization themselves run in different tasks.
        async def drive() -> None:
            try:
                await batch._run(cases, limit=limit)
            except BaseException as error:
                # An internal driver error must wake every result waiter as well as
                # propagate at fixture teardown; otherwise pytest waits until timeout.
                batch._finish_pending(error)
                raise

        driver = asyncio.create_task(drive())
        try:
            yield batch
        finally:
            driver.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await driver
            finally:
                # The driver can be cancelled before its coroutine starts.
                batch._finish_pending(asyncio.CancelledError("case batch closed"))


def selected_items(request: pytest.FixtureRequest) -> tuple[pytest.Function, ...]:
    """Collected items for this module, after pytest has applied selection."""
    return tuple(
        item for item in request.session.items if isinstance(item, pytest.Function) and item.parent is request.node
    )
