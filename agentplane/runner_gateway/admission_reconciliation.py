"""Settles pending command submissions from their runners' journals, without copying history.

A runner journals every admission it makes. When its reply to the submitter is lost, nothing else
records the admission: the submission stays pending until something reads the journal. This reads
only the journals of Sessions with pending submissions, from the Session's reconciliation cursor,
and advances that cursor; it stores no history and runs whether or not history ingestion is on.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.exc import SQLAlchemyError

from agentplane.grpc_options import grpc_channel_option_kvps
from agentplane.protocol import event_log_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.runner_gateway.commands.store import SubmissionConflictError, SubmissionStore
from agentplane.runner_gateway.session_registry import HistoryLocator, SessionRegistry
from agentplane.sandbox_service.destinations import DestinationResolver, DestinationUnavailableError
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination
from util.agent_sandbox import OperatingMode

# Generated stubs require the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


@asynccontextmanager
async def journal_reader(
    destinations: DestinationResolver, locator: HistoryLocator, *, runner_grpc_channel_options: dict[str, int | str]
) -> AsyncIterator[RunnerClient | None]:
    """A client for the runner holding the Session's journal, or None while the runner has not
    opened it. Reads only: following a journal never starts or resumes a harness."""
    view = await destinations.inventory.get(locator.sandbox_name)
    if view.uid != str(locator.sandbox_uid):
        # An old incarnation: never bind its journal to a replacement Pod.
        yield None
        return
    destination = SandboxDestination(
        sandbox=locator.sandbox_name, sandbox_uid=str(locator.sandbox_uid), owner=view.service_account
    )
    endpoint = await destinations.resolve(destination, writes=False)
    client = RunnerClient(
        grpc.aio.insecure_channel(endpoint.target, options=grpc_channel_option_kvps(runner_grpc_channel_options))
    )
    try:
        sessions = await asyncio.wait_for(client.list_sessions(), timeout=15)
        # A reservation without a runner Open has no journal yet.
        yield client if any(row.session_id == locator.runner_session_id for row in sessions) else None
    finally:
        await client.close()


async def reconcile_admissions(
    submissions: SubmissionStore, locator: HistoryLocator, runner: RunnerClient, *, batch_size: int = 128
) -> int:
    """Reconcile the journal published at attach time, from the Session's cursor; returns the new
    cursor. An interruption leaves the committed prefix for the next call."""
    if batch_size < 1:
        raise ValueError("batch size must be positive")
    cursor = await submissions.read_reconciled_through(locator.session_id)
    attachment = await runner.attach(locator.runner_session_id, after_cursor=cursor)
    try:
        through = attachment.attached.last_cursor
        if through < cursor:
            raise SubmissionConflictError(f"runner journal regressed below reconciled cursor {cursor}")
        while cursor < through:
            batch: list[event_log_pb2.EventEntry] = []
            while len(batch) < batch_size and cursor < through:
                try:
                    entry = await attachment.next_entry()
                except StreamClosedError as error:
                    raise ConnectionError("runner closed before replaying its published journal") from error
                if entry.cursor != cursor + 1:
                    raise SubmissionConflictError(f"runner replay expected cursor {cursor + 1}, got {entry.cursor}")
                batch.append(entry)
                cursor = entry.cursor
            await submissions.reconcile(locator.session_id, batch)
        return through
    finally:
        attachment.cancel()


class AdmissionReconciler:
    """Polls for pending submissions and reconciles their Sessions' journals.

    Replicas may reconcile the same Session at once: `SubmissionStore.reconcile` serializes them on
    the cursor row and skips entries already reconciled.
    """

    def __init__(
        self,
        submissions: SubmissionStore,
        sessions: SessionRegistry,
        destinations: DestinationResolver,
        *,
        runner_grpc_channel_options: dict[str, int | str],
        interval_s: float = 15,
        concurrency: int = 4,
    ) -> None:
        if interval_s <= 0 or concurrency < 1:
            raise ValueError("interval and concurrency must be positive")
        self.submissions = submissions
        self.sessions = sessions
        self.destinations = destinations
        self.runner_grpc_channel_options = runner_grpc_channel_options
        self.interval_s = interval_s
        self.concurrency = concurrency

    async def reconcile_one(self, locator: HistoryLocator) -> None:
        async with journal_reader(
            self.destinations, locator, runner_grpc_channel_options=self.runner_grpc_channel_options
        ) as runner:
            if runner is None:
                return
            # Bounds each pass, not the runner stream; a timeout keeps the committed prefix.
            async with asyncio.timeout(60):
                await reconcile_admissions(self.submissions, locator, runner)

    async def cycle(self) -> None:
        pending = await self.submissions.read_pending_sessions()
        if not pending:
            return
        sandboxes = await self.destinations.inventory.list_sandboxes()
        names = [row.name for row in sandboxes if row.operating_mode == OperatingMode.RUNNING]
        locators = [
            locator
            for locator in await self.sessions.runnable_locators(self.destinations.inventory.namespace, names)
            if locator.session_id in pending
        ]
        limit = asyncio.Semaphore(self.concurrency)

        async def guarded(locator: HistoryLocator) -> None:
            async with limit:
                try:
                    await self.reconcile_one(locator)
                except SandboxNotFoundError, DestinationUnavailableError:
                    return  # suspended or deleted: its submissions stay pending until it runs again
                except asyncio.CancelledError:
                    raise
                except (
                    SubmissionConflictError,
                    RunnerError,
                    SQLAlchemyError,
                    grpc.RpcError,
                    ConnectionError,
                    TimeoutError,
                    OSError,
                    k8s_client.ApiException,
                ):
                    logger.exception("Session %s admission reconciliation failed", locator.session_id)

        async with asyncio.TaskGroup() as tasks:
            for locator in locators:
                tasks.create_task(guarded(locator))

    async def run(self) -> None:
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError, k8s_client.ApiException:
                logger.exception("Pending submission discovery failed")
            await asyncio.sleep(self.interval_s)
