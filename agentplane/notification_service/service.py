"""Authorized subscriptions and bounded, recoverable background work. No app dependency."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import grpc
import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

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
from agentplane.notification_service.sources.github import (
    GitHub,
    GitHubAccessError,
    GitHubRetryError,
    GitHubSourceChangedError,
    GitHubUnavailableError,
)
from agentplane.notification_service.store import ClaimLostError, ConflictError, QuotaError, Store
from agentplane.protocol import command_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.sandbox_service.client import ReconnectRequiredError, Runner, SandboxServiceClient
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination, ServiceAccount
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
        notice_debounce: NoticeDebounceSettings,
        stale_confirmation_s: float,
        operator_reader_account: str | None = None,
    ) -> None:
        self.store, self.actions, self.sandboxes = store, actions, sandboxes
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

    async def deliver(self, claim: Inbox, runner: Runner, notice: Notice) -> None:
        # TODO: Observe command-scoped admission/delivery outcomes through Sandbox Service,
        # resumable by command ID, rather than checkpointing the shared conversation journal.
        attachment = await runner.attach(claim.session_id, after_cursor=max(0, notice.runner_cursor - 1))
        try:
            if attachment.attached.last_cursor < notice.runner_cursor:
                raise ConflictError("runner cursor regressed")
            # Verify the durable boundary before advancing, including after a worker restart.
            copied = notice.runner_cursor
            try:
                if copied:
                    await self.store.receipt(claim, notice, await attachment.next_entry())
                if not notice.attempted and attachment.attached.last_cursor > copied:
                    # No command can precede the durable attempt marker. Skip unrelated history,
                    # but retain the exact tail entry to verify continuity on an uncertain retry.
                    baseline = attachment.attached.last_cursor
                    attachment.cancel()
                    attachment = await runner.attach(claim.session_id, after_cursor=baseline - 1)
                    if attachment.attached.last_cursor < baseline:
                        raise ConflictError("runner cursor regressed")
                    entry = await attachment.next_entry()
                    if entry.cursor != baseline:
                        raise ConflictError("runner history has a gap")
                    await self.store.checkpoint_before_attempt(claim, notice, entry)
                    copied = baseline
                # Catch up only since the checkpoint, with bounded work per claim. Never skip
                # history after an attempt: it may contain a lost admission or confirmation.
                for _ in range(128):
                    if copied >= attachment.attached.last_cursor:
                        break
                    entry = await attachment.next_entry()
                    await self.store.receipt(claim, notice, entry)
                    copied = entry.cursor
            except StreamClosedError as failure:
                raise ConflictError("runner history ended before its promised cursor") from failure
            if copied < attachment.attached.last_cursor:
                return
            # Re-read durable receipt state. A lost response is uncertain: reconcile
            # the runner journal first, then retry only the same command ID/payload.
            # Runner admission is idempotent; never prepare a second notice here.
            current = await self.store.notice(claim, prepare=False)
            if current is None or current.command_id != notice.command_id or current.confirmed or current.error:
                return
            if attachment.attached.harness_state != runner_pb2.HARNESS_STATE_RUNNING:
                return
            if not current.admitted and (current.attempted or await self.store.attempt(claim, current)):
                await runner.command(
                    claim.session_id,
                    command_pb2.Command(
                        command_id=str(current.command_id), submit_input=command_pb2.SubmitInput(text=current.text)
                    ),
                    after_cursor=copied,
                )
            # Read through this attachment so the checkpoint remains contiguous, rather than jumping
            # directly to the command RPC's admission cursor and skipping a coalesced confirmation.
            with suppress(TimeoutError, StreamClosedError, ReconnectRequiredError):
                async with asyncio.timeout(2):
                    for _ in range(128):
                        await self.store.receipt(claim, current, await attachment.next_entry())
        finally:
            attachment.cancel()

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
            StreamClosedError,
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
