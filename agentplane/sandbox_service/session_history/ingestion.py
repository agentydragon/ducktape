"""Bounded, replayable runner → Sandbox Service history copy; independent of app folds.

Disabled by default until existing histories are imported and their prefixes checked.
Every replica can replay the same prefix: Store.append locks the Session row and
accepts exact duplicates but refuses gaps, divergent payloads and source changes.
"""

import asyncio
import logging

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.exc import SQLAlchemyError

from agentplane.grpc_options import grpc_channel_option_kvps
from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.sandbox_service.destinations import DestinationResolver, DestinationUnavailableError
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination, SessionFeedState
from agentplane.sandbox_service.session_history.store import HistoryConflictError, HistoryLocator, Store
from util.agent_sandbox import OperatingMode

# Generated stubs require the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


async def copy_confirmed_prefix(
    store: Store, locator: HistoryLocator, runner: RunnerClient, *, batch_size: int = 128
) -> int:
    """Copy the prefix published at attach time; never start or resume a native harness.

    No speculative read or cursor advancement: only the exact published EventEntry
    bytes commit. An interruption leaves a durable prefix for the next call to replay.
    """
    if batch_size < 1:
        raise ValueError("batch size must be positive")
    cursor, _ = await store.read(locator.session_id, limit=1)
    attachment = await runner.attach(locator.runner_session_id, after_cursor=cursor)
    try:
        through = attachment.attached.last_cursor
        if through < cursor:
            raise HistoryConflictError(f"runner history regressed below copied cursor {cursor}")
        while cursor < through:
            batch: list[event_log_pb2.EventEntry] = []
            while len(batch) < batch_size and cursor < through:
                try:
                    entry = await attachment.next_entry()
                except StreamClosedError as error:
                    raise ConnectionError("runner closed before replaying its published prefix") from error
                if entry.cursor != cursor + 1 or entry.cursor > through:
                    raise HistoryConflictError(f"runner replay expected cursor {cursor + 1}, got {entry.cursor}")
                batch.append(entry)
                cursor = entry.cursor
            await store.append(locator.session_id, batch)
        feed = SessionFeedState(attached=attachment.attached)
        await store.record_feed_state(locator.session_id, feed)
        if (
            feed.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
            and feed.attached.setup_state != runner_pb2.SETUP_STATE_RUNNING
        ):
            # A stopped snapshot alone is not EOF. A racing new event is replayed
            # on the next cycle; never advance a cursor merely by observing it.
            try:
                async with asyncio.timeout(1):
                    await attachment.next_entry()
            except StreamClosedError:
                feed.ended = True
                await store.record_feed_state(locator.session_id, feed)
            except TimeoutError:
                pass  # bounded probe, not terminal lifecycle evidence
        return through
    finally:
        attachment.cancel()


class HistoryIngester:
    """Best-effort shadow copier; missing Sandboxes retain their already copied Events.

    Polling is bounded to an inventory snapshot, not a long-lived unbounded Follow.
    Exact replay and a single DB row lock make concurrent replicas safe. A conflict is
    reported loudly; no replica can silently fork a copied Session prefix.
    """

    def __init__(
        self,
        store: Store,
        destinations: DestinationResolver,
        *,
        runner_grpc_channel_options: dict[str, int | str],
        interval_s: float = 15,
        concurrency: int = 4,
    ) -> None:
        self.store = store
        self.destinations = destinations
        self.runner_grpc_channel_options = runner_grpc_channel_options
        if interval_s <= 0 or concurrency < 1:
            raise ValueError("interval and concurrency must be positive")
        self.interval_s = interval_s
        self.concurrency = concurrency

    async def copy_one(self, locator: HistoryLocator) -> None:
        view = await self.destinations.inventory.get(locator.sandbox_name)
        if view.uid != str(locator.sandbox_uid):
            return  # old incarnation; never bind its journal to a replacement Pod
        destination = SandboxDestination(
            sandbox=locator.sandbox_name, sandbox_uid=str(locator.sandbox_uid), owner=view.service_account
        )
        endpoint = await self.destinations.resolve(destination)
        channel = grpc.aio.insecure_channel(
            endpoint.target, options=grpc_channel_option_kvps(self.runner_grpc_channel_options)
        )
        client = RunnerClient(channel)
        try:
            sessions = await asyncio.wait_for(client.list_sessions(), timeout=15)
            if not any(row.session_id == locator.runner_session_id for row in sessions):
                return  # reservation without a runner Open; no retry or false failure
            # Bound each runner read and DB batch without imposing a lifetime on the
            # runner stream. A timeout can leave a committed prefix for the next cycle.
            async with asyncio.timeout(60):
                await copy_confirmed_prefix(self.store, locator, client)
        finally:
            await client.close()

    async def cycle(self) -> None:
        sandboxes = await self.destinations.inventory.list_sandboxes()
        names = [row.name for row in sandboxes if row.operating_mode == OperatingMode.RUNNING and not row.deleting]
        locators = await self.store.runnable_locators(self.destinations.inventory.namespace, names)
        limit = asyncio.Semaphore(self.concurrency)

        async def guarded(locator: HistoryLocator) -> None:
            async with limit:
                try:
                    await self.copy_one(locator)
                except SandboxNotFoundError, DestinationUnavailableError:
                    return  # suspended or deleted: retain the existing prefix
                except asyncio.CancelledError:
                    raise
                except (
                    HistoryConflictError,
                    RunnerError,
                    SQLAlchemyError,
                    grpc.RpcError,
                    ConnectionError,
                    TimeoutError,
                    OSError,
                    k8s_client.ApiException,
                ):
                    logger.exception("Session %s history ingestion failed", locator.session_id)

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
                logger.exception("Session history discovery failed")
            await asyncio.sleep(self.interval_s)
