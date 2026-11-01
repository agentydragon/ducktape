"""Selected pytest cases sharing a deployment, with bounded case-owned async work."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

import pytest
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Status, StatusCode

from agentplane.system_tests.tracing import tracer


@dataclass(frozen=True)
class Case:
    nodeid: str
    run: Callable[[], Awaitable[None]]


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

    async def _run_case(self, name: str, case: Case) -> None:
        error: BaseException | None = None
        with tracer.start_as_current_span(
            "system_test.case", context=Context(), attributes={"pytest.nodeid": case.nodeid}
        ) as span:
            try:
                with tracer.start_as_current_span("system_test.scenario", attributes={"pytest.nodeid": case.nodeid}):
                    await case.run()
            except (Exception, pytest.fail.Exception, pytest.skip.Exception, pytest.xfail.Exception) as caught:
                # The matching pytest item re-raises this after its owned cleanup completes.
                error = caught
                span.set_status(Status(StatusCode.ERROR, str(caught)))
        assert isinstance(span, ReadableSpan)
        assert span.start_time is not None
        assert span.end_time is not None
        self._results[name].set_result(Result(error=error, seconds=(span.end_time - span.start_time) / 1e9))

    async def _run(self, cases: Mapping[str, Case], *, limit: int, exclusive: str | None) -> None:
        if exclusive is not None:
            await self._run_case(exclusive, cases[exclusive])
            if (await self.result(exclusive)).error is not None:
                for name, future in self._results.items():
                    if name != exclusive:
                        future.set_result(
                            Result(error=RuntimeError(f"Not run: exclusive case {exclusive} failed"), seconds=0)
                        )
                return
        slots = asyncio.Semaphore(limit)

        async def bounded(name: str, case: Case) -> None:
            async with slots:
                await self._run_case(name, case)

        async with asyncio.TaskGroup() as tasks:
            for name, case in cases.items():
                if name != exclusive:
                    tasks.create_task(bounded(name, case), name=name)

    @classmethod
    @asynccontextmanager
    async def running(cls, cases: Mapping[str, Case], *, limit: int, exclusive: str | None) -> AsyncIterator[CaseBatch]:
        if limit < 1:
            raise ValueError("case concurrency must be positive")
        batch = cls(cases)

        # The driver owns its TaskGroup entirely within one task; pytest async fixture
        # setup and finalization themselves run in different tasks.
        async def drive() -> None:
            try:
                await batch._run(cases, limit=limit, exclusive=exclusive)
            except Exception as error:
                # An internal driver error must wake every result waiter as well as
                # propagate at fixture teardown; otherwise pytest waits until timeout.
                for future in batch._results.values():
                    if not future.done():
                        future.set_result(Result(error=error, seconds=0))
                raise

        driver = asyncio.create_task(drive())
        try:
            yield batch
        finally:
            driver.cancel()
            with suppress(asyncio.CancelledError):
                await driver
