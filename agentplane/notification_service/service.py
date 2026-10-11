"""Authorized subscriptions and bounded, recoverable background work. No app dependency."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import grpc
import httpx
from more_itertools import first
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from agentplane.history_service.client import HistoryServiceClient
from agentplane.notification_service.db import Inbox, Notice
from agentplane.notification_service.github_state import RefreshDeferredError
from agentplane.notification_service.models import (
    ActionsSource,
    DestinationRef,
    SourceFailureKind,
    Subscribe,
    SubscriptionView,
)
from agentplane.notification_service.settings import NoticeDebounceSettings
from agentplane.notification_service.sources.actions import Actions, SourceNotOwnedError
from agentplane.notification_service.sources.github import GitHub
from agentplane.notification_service.sources.github_client import (
    GitHubAccessError,
    GitHubRetryError,
    GitHubSourceChangedError,
    GitHubUnavailableError,
)
from agentplane.notification_service.store import ClaimLostError, ConflictError, QuotaError, Store
from agentplane.proto import command_pb2, event_log_pb2, runner_pb2
from agentplane.proto.sandbox_service_pb2 import SandboxDestination, ServiceAccount
from agentplane.runner.errors import RunnerError
from agentplane.sandbox_service.client import Runner, SandboxServiceClient
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipal

# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


def source_failure_kind(failure: Exception) -> SourceFailureKind:
    if isinstance(failure, GitHubRetryError):
        return SourceFailureKind.RATE_LIMITED
    if isinstance(failure, (GitHubAccessError, SourceNotOwnedError)):
        return SourceFailureKind.ACCESS_DENIED
    if isinstance(failure, GitHubSourceChangedError):
        return SourceFailureKind.SOURCE_CHANGED
    if isinstance(failure, (GitHubUnavailableError, httpx.TransportError)):
        return SourceFailureKind.UNAVAILABLE
    if isinstance(failure, httpx.HTTPStatusError):
        if failure.response.status_code == 429:
            return SourceFailureKind.RATE_LIMITED
        if failure.response.status_code in (401, 403, 404):
            return SourceFailureKind.ACCESS_DENIED
        if failure.response.status_code >= 500:
            return SourceFailureKind.UNAVAILABLE
    return SourceFailureKind.PROCESSING_ERROR


class DestinationRejectedError(Exception):
    pass


class OwnerMismatchError(DestinationRejectedError):
    pass


class StaleDestinationError(DestinationRejectedError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class Service:
    def __init__(
        self,
        store: Store,
        actions: Actions,
        sandboxes: SandboxServiceClient,
        github: GitHub | None = None,
        *,
        history: HistoryServiceClient,
        notice_debounce: NoticeDebounceSettings,
        stale_confirmation_s: float,
        operator_reader_account: str | None = None,
    ) -> None:
        self.store, self.actions, self.sandboxes, self.history = store, actions, sandboxes, history
        self.github = github
        self.operator_reader_account = operator_reader_account
        self.notice_debounce = notice_debounce
        self.stale_confirmation_s = stale_confirmation_s

    async def runner(self, owner: ServiceAccountRef, destination: DestinationRef) -> Runner:
        if destination.namespace != self.sandboxes.namespace:
            raise DestinationRejectedError
        try:
            sandbox = await self.sandboxes.get(destination.name)
        except SandboxNotFoundError as error:
            raise StaleDestinationError("not_found") from error
        if sandbox.uid != destination.uid:
            raise StaleDestinationError("uid_changed")
        if sandbox.service_account != ServiceAccount(namespace=owner.namespace, name=owner.name):
            raise OwnerMismatchError
        return self.sandboxes.runner(
            SandboxDestination(sandbox=destination.name, sandbox_uid=destination.uid, owner=sandbox.service_account)
        )

    async def subscribe(self, principal: WorkloadPrincipal, body: Subscribe) -> SubscriptionView:
        runner = await self.runner(principal.account, body.destination_ref)
        if body.session_id not in {session.session_id for session in await runner.list_sessions()}:
            raise DestinationRejectedError
        # Authorize source access before persistence, even when replay starts after the last event.
        if isinstance(body.source, ActionsSource):
            await self.actions.events(principal.account, body.source.request_id, body.source.after_sequence)
            return await self.store.subscribe(principal, body)
        if self.github is None:
            raise GitHubUnavailableError("GitHub provider is disabled")
        context = await self.github.context(body.source)
        return await self.store.subscribe(principal, body, context.binding)

    async def read_history(self, session_id: str, *, after_cursor: int, limit: int) -> list[event_log_pb2.EventEntry]:
        """Entries after `after_cursor` as the History Service has committed them."""
        page = await self.history.read_session_events(session_id, after_cursor=after_cursor, limit=limit)
        # Settlement drops streamed deltas only; a missing cursor nothing settled could be a receipt.
        settled = [(span.first, span.last) for settlement in page.settlements for span in settlement.ranges]
        previous = after_cursor
        for entry in page.entries:
            if not all(
                any(low <= cursor <= high for low, high in settled) for cursor in range(previous + 1, entry.cursor)
            ):
                raise ConflictError("runner history has a gap")
            previous = entry.cursor
        return list(page.entries)

    async def deliver(self, claim: Inbox, runner: Runner, notice: Notice) -> None:
        # TODO: Observe command-scoped admission/delivery outcomes through Sandbox Service,
        # resumable by command ID, rather than checkpointing the shared conversation journal.
        # The runner's journal end now; its entries are read from the History Service, which may lag.
        live = first(
            (session for session in await runner.list_sessions() if session.session_id == claim.session_id), None
        )
        if live is None:
            raise DestinationRejectedError
        if live.last_cursor < notice.runner_cursor:
            raise ConflictError("runner cursor regressed")
        copied = notice.runner_cursor
        # Verify the durable boundary before advancing, including after a worker restart.
        if copied:
            boundary = await self.read_history(claim.session_id, after_cursor=copied - 1, limit=1)
            if not boundary:
                return
            await self.store.receipt(claim, notice, boundary[0], after_cursor=copied - 1)
            copied = boundary[0].cursor
        if not notice.attempted and live.last_cursor > copied:
            # No command can precede the durable attempt marker. Skip unrelated history,
            # but retain the exact tail entry to verify continuity on an uncertain retry.
            tail = await self.read_history(claim.session_id, after_cursor=live.last_cursor - 1, limit=1)
            if not tail:
                return
            await self.store.checkpoint_before_attempt(claim, notice, tail[0])
            copied = tail[0].cursor
        # Catch up only since the checkpoint, with bounded work per claim. Never skip
        # history after an attempt: it may contain a lost admission or confirmation.
        for entry in await self.read_history(claim.session_id, after_cursor=copied, limit=128):
            await self.store.receipt(claim, notice, entry, after_cursor=copied)
            copied = entry.cursor
        # Until the History Service has everything the runner had, a retry could precede an admission.
        if copied < live.last_cursor:
            return
        # Re-read durable receipt state. A lost response is uncertain: reconcile
        # the runner journal first, then retry only the same command ID/payload.
        # Runner admission is idempotent; never prepare a second notice here.
        current = await self.store.notice(claim, prepare=False)
        if current is None or current.command_id != notice.command_id or current.confirmed or current.error:
            return
        if live.harness_state != runner_pb2.HARNESS_STATE_RUNNING:
            return
        if not current.admitted and (current.attempted or await self.store.attempt(claim, current)):
            # Its admission and confirmation are copied by a later claim, once the History Service has them.
            await self.sandboxes.command(
                claim.session_id,
                command_pb2.Command(
                    command_id=str(current.command_id), submit_input=command_pb2.SubmitInput(text=current.text)
                ),
            )

    async def get_notice_due_at(self, claim: Inbox) -> datetime | None:
        pending = await self.store.get_pending_entry_times(claim)
        if pending is None:
            return None
        first, last = pending
        # Use durable entry times, not a process-local timer. Overlapping matches do not extend it.
        return min(
            last + timedelta(seconds=self.notice_debounce.quiet_seconds),
            first + timedelta(seconds=self.notice_debounce.max_wait_seconds),
        )

    async def prepare_notice(self, claim: Inbox) -> Notice | None:
        due_at = await self.get_notice_due_at(claim)
        notice = await self.store.notice(claim, prepare=due_at is not None and due_at <= datetime.now(UTC))
        # Debounce only new notices, never retry/confirmation of an existing command.
        if notice is not None and not notice.confirmed and notice.error is None:
            return notice
        return None

    async def step(self) -> bool:
        claim = await self.store.claim()
        if claim is None:
            return False
        error = (
            claim.delivery_error
            if claim.delivery_error and claim.delivery_error.startswith("invalid history")
            else None
        )
        stale_observed = False
        try:
            # Leave ten seconds for fenced state recording; a stale worker can do no further commits.
            async with asyncio.timeout(20):
                owner = ServiceAccountRef(namespace=claim.owner_namespace, name=claim.owner_name)
                runner = await self.runner(owner, DestinationRef.model_validate(claim.destination_ref))
                await self.store.clear_stale(claim)
                source = await self.store.source(claim)
                if source is not None:
                    try:
                        spec = Subscribe.model_validate(source.creation).source
                        if isinstance(spec, ActionsSource):
                            assert source.actions_after_sequence is not None
                            events = await self.actions.events(owner, spec.request_id, source.actions_after_sequence)
                            await self.store.record(claim, source, events)
                        elif self.github is None:
                            raise GitHubUnavailableError("GitHub provider is disabled")
                        else:
                            await self.github.reconcile(self.store, claim, source, spec)
                    except RefreshDeferredError as deferred:
                        await self.store.source_deferred(claim, source, deferred.until)
                    except (
                        httpx.HTTPError,
                        ValidationError,
                        SourceNotOwnedError,
                        QuotaError,
                        ConflictError,
                        GitHubUnavailableError,
                        GitHubRetryError,
                    ) as failure:
                        # No upstream body, bearer, or native content in diagnostics.
                        source_error = (
                            str(failure)
                            if isinstance(failure, (GitHubUnavailableError, GitHubRetryError))
                            else (
                                f"HTTP {failure.response.status_code}"
                                if isinstance(failure, httpx.HTTPStatusError)
                                else type(failure).__name__
                            )
                        )
                        retry_seconds = failure.retry_seconds if isinstance(failure, GitHubRetryError) else 60
                        await self.store.source_failed(
                            claim, source, source_error, retry_seconds, kind=source_failure_kind(failure)
                        )
                        logger.warning(
                            "notification source retry: inbox=%s subscription=%s cause=%s retry_seconds=%s",
                            claim.id,
                            source.id,
                            source_error,
                            retry_seconds,
                        )
                notice = await self.prepare_notice(claim)
                if notice is not None and error is None:
                    await self.deliver(claim, runner, notice)
        except StaleDestinationError as failure:
            with suppress(ClaimLostError):
                retired = await self.store.observe_stale(claim, failure.reason, self.stale_confirmation_s)
                if retired:
                    logger.info("retired stale notification inbox: inbox=%s reason=%s", claim.id, failure.reason)
            stale_observed = True
        except OwnerMismatchError:
            with suppress(ClaimLostError):
                await self.store.clear_stale(claim)
            error = "OwnerMismatchError"
            logger.warning("notification delivery unavailable: inbox=%s cause=%s", claim.id, error)
        except ConflictError as failure:
            error = f"invalid history: {failure}"
        except ClaimLostError:
            return True
        except (
            ConnectionError,
            TimeoutError,
            grpc.aio.AioRpcError,
            RunnerError,
            DestinationRejectedError,
            QuotaError,
        ) as failure:
            error = error or type(failure).__name__
            logger.warning("notification delivery unavailable: inbox=%s cause=%s", claim.id, error)
        finally:
            if not stale_observed:
                with suppress(ClaimLostError):
                    await self.store.release(claim, error, notice_due_at=await self.get_notice_due_at(claim))
        return True

    async def run(self) -> None:
        next_cleanup = 0.0
        cleanup_cursor = None
        with self.store.wakeups.subscribe() as changed:
            while True:
                # Clear before reading: a commit racing with either query remains a wakeup.
                changed.clear()
                try:
                    now = asyncio.get_running_loop().time()
                    if now >= next_cleanup:
                        cleanup_cursor = await self.store.cleanup(cleanup_cursor)
                        next_cleanup = now + 60
                    if await self.step():
                        continue
                    next_work_at = await self.store.get_next_work_at()
                    timeout = max(0, next_cleanup - asyncio.get_running_loop().time())
                    if next_work_at is not None:
                        timeout = min(timeout, max(0, (next_work_at - datetime.now(UTC)).total_seconds()))
                    with suppress(TimeoutError):
                        async with asyncio.timeout(timeout):
                            await changed.wait()
                except SQLAlchemyError:
                    logger.warning("notification storage unavailable; retrying", exc_info=True)
                    await asyncio.sleep(5)
