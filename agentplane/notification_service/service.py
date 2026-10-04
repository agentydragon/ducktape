"""Authorized subscriptions and bounded, recoverable background work. No app dependency."""

import asyncio
import logging
from contextlib import suppress

import grpc
import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from agentplane.notification_service.db import Inbox, Notice
from agentplane.notification_service.models import DestinationRef, Subscribe, SubscriptionView
from agentplane.notification_service.sources.actions import Actions, SourceNotOwnedError
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


class DestinationRejectedError(Exception):
    pass


class Service:
    def __init__(self, store: Store, actions: Actions, sandboxes: SandboxServiceClient) -> None:
        self.store, self.actions, self.sandboxes = store, actions, sandboxes

    async def runner(self, owner: ServiceAccountRef, destination: DestinationRef) -> Runner:
        if destination.namespace != self.sandboxes.namespace:
            raise DestinationRejectedError
        try:
            sandbox = await self.sandboxes.get(destination.name)
        except SandboxNotFoundError as error:
            raise DestinationRejectedError from error
        if sandbox.uid != destination.uid or sandbox.service_account != ServiceAccount(
            namespace=owner.namespace, name=owner.name
        ):
            raise DestinationRejectedError
        return self.sandboxes.runner(
            SandboxDestination(sandbox=destination.name, sandbox_uid=destination.uid, owner=sandbox.service_account)
        )

    async def subscribe(self, principal: WorkloadPrincipal, body: Subscribe) -> SubscriptionView:
        runner = await self.runner(principal.account, body.destination_ref)
        if body.session_id not in {session.session_id for session in await runner.list_sessions()}:
            raise DestinationRejectedError
        # Authorize source access before persistence, even when replay starts after the last event.
        await self.actions.events(principal.account, body.source.request_id, body.source.after_sequence)
        return await self.store.subscribe(principal, body)

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
            # Re-read durable receipt state. An admitted command with no confirmation is uncertain,
            # not permission to generate another command or remind the agent.
            current = await self.store.notice(claim, prepare=False)
            if current is None or current.command_id != notice.command_id or current.confirmed or current.error:
                return
            if attachment.attached.harness_state != runner_pb2.HARNESS_STATE_RUNNING:
                return
            if not current.admitted and await self.store.attempt(claim, current):
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

    async def step(self) -> bool:
        claim = await self.store.claim()
        if claim is None:
            return False
        error = (
            claim.delivery_error
            if claim.delivery_error and claim.delivery_error.startswith("invalid history")
            else None
        )
        try:
            # Leave ten seconds for fenced state recording; a stale worker can do no further commits.
            async with asyncio.timeout(20):
                owner = ServiceAccountRef(namespace=claim.owner_namespace, name=claim.owner_name)
                runner = await self.runner(owner, DestinationRef.model_validate(claim.destination_ref))
                source = await self.store.source(claim)
                if source is not None:
                    try:
                        events = await self.actions.events(owner, source.request_id, source.after_sequence)
                        await self.store.record(claim, source, events)
                    except (
                        httpx.HTTPError,
                        ValidationError,
                        SourceNotOwnedError,
                        QuotaError,
                        ConflictError,
                    ) as failure:
                        # No upstream body, bearer, or native content in diagnostics.
                        await self.store.record(
                            claim,
                            source,
                            [],
                            f"HTTP {failure.response.status_code}"
                            if isinstance(failure, httpx.HTTPStatusError)
                            else type(failure).__name__,
                        )
                notice = await self.store.notice(claim)
                if notice is not None and error is None:
                    await self.deliver(claim, runner, notice)
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
            with suppress(ClaimLostError):
                await self.store.release(claim, error)
        return True

    async def run(self) -> None:
        next_cleanup = 0.0
        cleanup_cursor = None
        while True:
            try:
                now = asyncio.get_running_loop().time()
                if now >= next_cleanup:
                    cleanup_cursor = await self.store.cleanup(cleanup_cursor)
                    next_cleanup = now + 60
                if not await self.step():
                    await asyncio.sleep(1)
            except SQLAlchemyError:
                logger.warning("notification storage unavailable; retrying", exc_info=True)
                await asyncio.sleep(5)
