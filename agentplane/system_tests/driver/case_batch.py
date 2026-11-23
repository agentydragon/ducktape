"""Selected pytest cases sharing a deployment, with bounded case-owned async work.

A replacement for this `TaskGroup` driver must keep session-scoped async fixtures alive until every case has
cleaned up, keep individual results and filtering, bound concurrency, and run cases that change deployment-wide
state exclusively. `pytest-asyncio-cooperative` 0.40.0 fails the first: it replaces fixture finalization with its
own reference counting, and the shared stack stopped while a case was still running. TestFlows 2.0.261028 does not
import on Python 3.14. `pytest-xdist` runs a session fixture once per worker, so it needs a parent-owned stack and
case routing in the shared scripted-model server.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass

import pytest
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode

from agentplane.system_tests.driver.cleanup import CleanupError
from agentplane.system_tests.driver.tracing import tracer


@dataclass(frozen=True)
class Case[Worker]:
    nodeid: str
    run: Callable[[Worker], Awaitable[None]]
    exclusive: bool = False
    timeout_seconds: float | None = None

    @classmethod
    def for_item(cls, item: pytest.Function, run: Callable[[Worker], Awaitable[None]]) -> Case[Worker]:
        timeout = item.get_closest_marker("case_timeout")
        seconds = float(timeout.args[0] if timeout.args else timeout.kwargs["seconds"]) if timeout else None
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
    def __init__(self, names: Iterable[str]) -> None:
        loop = asyncio.get_running_loop()
        self._results: dict[str, asyncio.Future[Result]] = {name: loop.create_future() for name in names}

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

    async def _run_case[Worker](self, name: str, case: Case[Worker], worker: Worker) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        error: BaseException | None = None
        with tracer.start_as_current_span(
            "system_test.case", context=Context(), attributes={"pytest.nodeid": case.nodeid}
        ) as span:
            try:
                with tracer.start_as_current_span("system_test.scenario", attributes={"pytest.nodeid": case.nodeid}):
                    async with asyncio.timeout(case.timeout_seconds):
                        await case.run(worker)
            except CleanupError:
                raise
            except BaseExceptionGroup as caught:
                # Preserve case outcomes, but abort on failed cleanup or process control.
                _, unhandled = caught.split(
                    (Exception, pytest.fail.Exception, pytest.skip.Exception, pytest.xfail.Exception)
                )
                if unhandled is not None or caught.subgroup(CleanupError) is not None:
                    raise
                error = caught
                span.set_status(Status(StatusCode.ERROR, str(caught)))
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
        self._results[name].set_result(Result(error=error, seconds=loop.time() - started))

    async def _run[Worker](
        self,
        cases: Mapping[str, Case[Worker]],
        *,
        limit: int,
        worker_scope: Callable[[], AbstractAsyncContextManager[Worker]],
    ) -> None:
        for name, case in cases.items():
            if not case.exclusive:
                continue
            async with worker_scope() as worker:
                await self._run_case(name, case, worker)
            if (await self.result(name)).error is not None:
                self._finish_pending(RuntimeError(f"Not run: exclusive case {name} failed"))
                return
        pending = iter((name, case) for name, case in cases.items() if not case.exclusive)

        async def work() -> None:
            task = asyncio.current_task()
            assert task is not None
            async with worker_scope() as worker:
                # Taking the next case has no await; each case belongs to one worker.
                for name, case in pending:
                    await self._run_case(name, case, worker)
                    if task.cancelling():
                        return

        async with asyncio.TaskGroup() as tasks:
            for _ in range(min(limit, sum(not case.exclusive for case in cases.values()))):
                tasks.create_task(work())

    @classmethod
    @asynccontextmanager
    async def running[Worker](
        cls,
        cases: Mapping[str, Case[Worker]],
        *,
        limit: int,
        worker_scope: Callable[[], AbstractAsyncContextManager[Worker]],
    ) -> AsyncIterator[CaseBatch]:
        if limit < 1:
            raise ValueError("case concurrency must be positive")
        batch = cls(cases)

        # The driver owns its TaskGroup entirely within one task; pytest async fixture
        # setup and finalization themselves run in different tasks.
        async def drive() -> None:
            try:
                await batch._run(cases, limit=limit, worker_scope=worker_scope)
            except BaseException as error:
                # An internal driver error must wake every result waiter as well as
                # propagate at fixture teardown; otherwise pytest waits until timeout.
                batch._finish_pending(error)
                raise
            finally:
                batch._finish_pending(asyncio.CancelledError("case workers stopped"))

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
