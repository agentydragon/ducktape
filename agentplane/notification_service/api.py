"""Workload-authenticated HTTP surface; no implicit current session and no destructive reads."""

import asyncio
import json
import logging
import traceback
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select

from agentplane.notification_service.channels import Channel
from agentplane.notification_service.models import (
    Acknowledge,
    InboxPage,
    InboxView,
    SandboxNotificationStatus,
    SessionNoticeStates,
    SourceView,
    Subscribe,
    SubscriptionUpdate,
    SubscriptionView,
)
from agentplane.notification_service.service import DestinationRejectedError, Service
from agentplane.notification_service.sources.actions import SourceNotOwnedError
from agentplane.notification_service.sources.github.client import GitHubRetryError, GitHubUnavailableError
from agentplane.notification_service.sources.github.source import (
    PAYLOAD_MODELS,
    InvalidSignatureError,
    MissingWebhookRepositoryError,
    UnsupportedWebhookEventError,
)
from agentplane.notification_service.store import MAX_NOTICE_STATE_SESSIONS, ConflictError, NotFoundError, QuotaError
from agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from agentplane.workload_auth.principal import WorkloadPrincipal, WorkloadPrincipalResolver

logger = logging.getLogger(__name__)


def notification_service(request: Request) -> Service:
    return cast(Service, request.app.state.notification_service)


def workload_authenticator(request: Request) -> WorkloadPrincipalAuthenticator:
    return cast(WorkloadPrincipalAuthenticator, request.app.state.workload_authenticator)


async def authenticated_caller(
    request: Request, authenticate: Annotated[WorkloadPrincipalAuthenticator, Depends(workload_authenticator)]
) -> WorkloadPrincipal:
    return await authenticate(request)


Caller = Annotated[WorkloadPrincipal, Depends(authenticated_caller)]
Notifications = Annotated[Service, Depends(notification_service)]
router = APIRouter()


@router.get("/readyz")
async def ready(request: Request, service: Notifications) -> dict[str, str]:
    workers = cast(list[asyncio.Task[None]], request.app.state.notification_workers)
    if not workers or any(worker.done() for worker in workers) or not service.store.wakeups.listener.connected:
        raise HTTPException(503, "notification workers unavailable")
    async with service.store.sessions() as session:
        await session.execute(select(1))
    return {"status": "ready"}


@router.get("/healthz")
async def health(request: Request) -> dict[str, str]:
    # A failed delivery worker cannot be repaired by HTTP readiness alone: it
    # must also fail liveness so Kubernetes replaces the still-running process.
    workers = cast(list[asyncio.Task[None]], request.app.state.notification_workers)
    if any(worker.done() for worker in workers):
        raise HTTPException(503, "notification workers failed")
    return {"status": "alive"}


def _authorize_sandbox_status(namespace: str, caller: WorkloadPrincipal, service: Service) -> None:
    _authorize_operator_reader(caller, service)
    if namespace != caller.namespace:
        raise HTTPException(404, "sandbox not found")


async def _sandbox_status(service: Service, namespace: str, name: str, uid: str) -> SandboxNotificationStatus:
    return await service.store.sandbox_status(
        namespace,
        name,
        uid,
        quiet_seconds=service.notice_debounce.quiet_seconds,
        max_wait_seconds=service.notice_debounce.max_wait_seconds,
    )


async def _status_frames[S: SandboxNotificationStatus | SessionNoticeStates](
    service: Service,
    read: Callable[[], Awaitable[S]],
    key: Callable[[S], str],
    deadlines: Callable[[S], Iterable[datetime | None]],
) -> AsyncGenerator[bytes]:
    """Subscribe before the first snapshot; NOTIFY invalidates, canonical rows are the source. A
    snapshot is sent when its `key` changes, and read again at its next deadline."""
    changed = asyncio.Event()
    with service.store.wakeups.changes[Channel.WORK].subscribe(changed):
        listener = service.store.wakeups.listener
        generation = listener.generation
        last: str | None = None
        while listener.connected and listener.generation == generation:
            changed.clear()
            status = await read()
            if (current := key(status)) != last:
                yield f"event: snapshot\ndata: {json.dumps(status.model_dump(mode='json'))}\n\n".encode()
                last = current
            now = datetime.now(UTC)
            due = [deadline for deadline in deadlines(status) if deadline is not None and deadline > status.observed_at]
            delay = max(0, (min(due) - now).total_seconds()) if due else None
            with suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), timeout=delay)


def sandbox_status_frames(service: Service, namespace: str, name: str, uid: str) -> AsyncGenerator[bytes]:
    def key(status: SandboxNotificationStatus) -> str:
        # observed_at is a read timestamp, not a state change.
        return json.dumps(status.model_dump(mode="json")["inboxes"], sort_keys=True) + json.dumps(
            [sub.expires_at <= status.observed_at for item in status.inboxes for sub in item.subscriptions]
        )

    return _status_frames(
        service,
        lambda: _sandbox_status(service, namespace, name, uid),
        key,
        lambda status: (
            deadline
            for item in status.inboxes
            for deadline in (item.notice_due_at, *(sub.expires_at for sub in item.subscriptions if not sub.cancelled))
        ),
    )


@router.get("/v1/operator/sandboxes/{namespace}/{name}/notifications/stream")
async def sandbox_notifications_stream(
    namespace: str, name: str, uid: str, caller: Caller, service: Notifications
) -> StreamingResponse:
    _authorize_sandbox_status(namespace, caller, service)
    return StreamingResponse(
        sandbox_status_frames(service, namespace, name, uid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _authorize_operator_reader(caller: WorkloadPrincipal, service: Service) -> None:
    if (
        caller.namespace != service.sandboxes.namespace
        or caller.service_account_name != service.operator_reader_account
        or service.operator_reader_account is None
    ):
        raise HTTPException(403, "operator diagnostics not authorized")


def session_notice_frames(service: Service, namespace: str, session_ids: set[str]) -> AsyncGenerator[bytes]:
    return _status_frames(
        service,
        lambda: service.store.session_notice_states(
            namespace,
            session_ids,
            quiet_seconds=service.notice_debounce.quiet_seconds,
            max_wait_seconds=service.notice_debounce.max_wait_seconds,
        ),
        lambda states: json.dumps(states.model_dump(mode="json")["inboxes"], sort_keys=True),
        lambda states: (inbox.eligible_at for inbox in states.inboxes),
    )


# No caller yet: staged for the Thread list's pending-notice indicator.
@router.get("/v1/operator/notices/stream")
async def session_notices_stream(
    caller: Caller,
    service: Notifications,
    session_id: Annotated[list[str], Query(max_length=MAX_NOTICE_STATE_SESSIONS)],
) -> StreamingResponse:
    """The named Sessions' live inboxes at a glance, in the operator's Sandbox namespace, pushed
    whenever they change and when a debounced notice falls due."""
    _authorize_operator_reader(caller, service)
    return StreamingResponse(
        session_notice_frames(service, caller.namespace, set(session_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/v1/sources")
async def sources(caller: Caller, service: Notifications) -> dict[str, SourceView]:
    result = {
        "actions": SourceView(
            subscription_schema=Subscribe.model_json_schema(), content="ActionEventView: sequence, state, at, actor"
        )
    }
    if service.github is not None:
        result["github"] = SourceView(
            subscription_schema=Subscribe.model_json_schema(), content="Full GitHub webhook JSON; identity in event"
        )
    return result


def log_webhook_rejection(event: str, delivery_id: UUID | None, reason: str, error: ValueError | None) -> None:
    # Header values are untrusted even when the body signature is valid. Bound and JSON-escape
    # the event; never retain a malformed delivery ID, signature, body or exception message.
    diagnostic: dict[str, object] = {
        "event": event[:64],
        "event_truncated": len(event) > 64,
        "delivery_id": str(delivery_id) if delivery_id is not None else None,
        "reason": reason,
    }
    if isinstance(error, ValidationError):
        model = PAYLOAD_MODELS.get(event)
        fields = model.model_fields if model is not None else {}
        diagnostic["error_count"] = error.error_count()
        # Locations below the schema's top-level fields can contain payload-controlled map keys.
        # Only report known top-level fields and Pydantic error codes, not messages or context.
        diagnostic["validation"] = [
            {"field": item["loc"][0] if item["loc"] and item["loc"][0] in fields else "<payload>", "type": item["type"]}
            for item in error.errors(include_input=False, include_context=False, include_url=False)[:8]
        ]
    logger.warning("GitHub webhook rejected: %s", json.dumps(diagnostic, ensure_ascii=True))


@router.post("/v1/webhooks/github", status_code=202)
async def github_webhook(request: Request, service: Notifications) -> dict[str, bool]:
    github = service.github
    if github is None:
        raise HTTPException(404, "GitHub provider is disabled")
    if github.ingress_slots.locked():
        raise HTTPException(503, "GitHub ingress busy", headers={"Retry-After": "5"})
    raw = bytearray()
    async with github.ingress_slots, asyncio.timeout(10):
        async for chunk in request.stream():
            if len(raw) + len(chunk) > github.settings.max_body_bytes:
                raise HTTPException(413, "GitHub payload exceeds configured limit")
            raw.extend(chunk)
        event = request.headers.get("x-github-event", "")
        delivery_id = None
        try:
            delivery_id = UUID(request.headers.get("x-github-delivery", ""))
            created = await github.ingest(
                service.store, event, delivery_id, request.headers.get("x-hub-signature-256", ""), bytes(raw)
            )
        except InvalidSignatureError as error:
            log_webhook_rejection(event, delivery_id, "invalid_signature", None)
            raise HTTPException(401, "invalid GitHub signature") from error
        except ValueError as error:
            if delivery_id is None:
                reason = "invalid_delivery_id"
            elif isinstance(error, UnsupportedWebhookEventError):
                reason = "unsupported_event"
            elif isinstance(error, MissingWebhookRepositoryError):
                reason = "missing_repository"
            elif isinstance(error, ValidationError):
                reason = "payload_validation"
            else:
                reason = "invalid_delivery"
            log_webhook_rejection(event, delivery_id, reason, error)
            raise HTTPException(400, "invalid GitHub delivery") from error
    return {"accepted": True, "duplicate": not created}


@router.post("/v1/subscriptions", response_model=SubscriptionView)
async def subscribe(body: Subscribe, caller: Caller, service: Notifications) -> SubscriptionView:
    try:
        return await service.subscribe(caller, body)
    except GitHubUnavailableError as error:
        raise HTTPException(403, str(error)) from error
    except GitHubRetryError as error:
        raise HTTPException(
            503, "GitHub temporarily unavailable", headers={"Retry-After": str(error.retry_seconds)}
        ) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(
            403 if error.response.status_code in (401, 403, 404) else 503, "Source unavailable or unauthorized"
        ) from error


@router.get("/v1/subscriptions")
async def subscriptions(caller: Caller, service: Notifications, after_id: UUID | None = None) -> list[SubscriptionView]:
    return await service.store.subscriptions(caller.account, after_id)


@router.get("/v1/subscriptions/{subscription_id}")
async def subscription(subscription_id: UUID, caller: Caller, service: Notifications) -> SubscriptionView:
    return await service.store.subscription(caller.account, subscription_id)


@router.patch("/v1/subscriptions/{subscription_id}")
async def update(
    subscription_id: UUID, body: SubscriptionUpdate, caller: Caller, service: Notifications
) -> SubscriptionView:
    return await service.store.change(caller.account, subscription_id, body)


@router.delete("/v1/subscriptions/{subscription_id}")
async def cancel(subscription_id: UUID, caller: Caller, service: Notifications) -> SubscriptionView:
    return await service.store.change(caller.account, subscription_id, None)


@router.get("/v1/inboxes")
async def inboxes(caller: Caller, service: Notifications) -> list[InboxView]:
    return await service.store.inboxes(caller.account)


@router.get("/v1/inboxes/{inbox_id}/entries")
async def read(
    inbox_id: UUID,
    caller: Caller,
    service: Notifications,
    after_cursor: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    limit: Annotated[int, Query(ge=1, le=128)] = 128,
) -> InboxPage:
    return await service.store.read(caller.account, inbox_id, after_cursor, limit)


@router.put("/v1/inboxes/{inbox_id}/acknowledgement")
async def acknowledge(inbox_id: UUID, body: Acknowledge, caller: Caller, service: Notifications) -> InboxView:
    return await service.store.acknowledge(caller.account, inbox_id, body.through_cursor)


@router.delete("/v1/inboxes/{inbox_id}", status_code=204)
async def retire(inbox_id: UUID, caller: Caller, service: Notifications) -> Response:
    await service.store.retire(caller.account, inbox_id)
    return Response(status_code=204)


async def missing(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": "resource not found or not owned by this account"}, status_code=404)


async def source_not_owned(request: Request, error: SourceNotOwnedError) -> JSONResponse:
    return JSONResponse({"detail": "Source unavailable or unauthorized"}, status_code=403)


async def conflict(request: Request, error: ConflictError) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=409)


async def quota(request: Request, error: QuotaError) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=429)


async def unavailable(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": "backend temporarily unavailable"}, status_code=503)


def _report_worker_exit(worker: asyncio.Task[None]) -> None:
    if worker.cancelled():
        return  # Expected on application shutdown.
    error = worker.exception()  # Retrieve it even while the task is retained by app.state.
    if error is None:
        logger.error("notification worker %s stopped unexpectedly", worker.get_name())
    else:
        # Do not print the exception message or locals: downstream exceptions can
        # contain delivery payloads or credentials. Preserve the stack and type.
        stack = "".join(traceback.format_list(traceback.extract_tb(error.__traceback__)))
        logger.error("notification worker %s failed: %s\n%s", worker.get_name(), type(error).__name__, stack)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    service = cast(Service, app.state.notification_service)
    async with service.store.wakeups.listener.listen():
        workers = [asyncio.create_task(service.run(), name=f"notifications-{i}") for i in range(4)]
        app.state.notification_workers = workers
        for worker in workers:
            worker.add_done_callback(_report_worker_exit)
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


def create_app(service: Service, principals: WorkloadPrincipalResolver) -> FastAPI:
    app = FastAPI(
        title="Agentplane notifications",
        lifespan=_lifespan,
        exception_handlers={
            NotFoundError: missing,
            DestinationRejectedError: missing,
            SourceNotOwnedError: source_not_owned,
            ConflictError: conflict,
            QuotaError: quota,
            ConnectionError: unavailable,
            TimeoutError: unavailable,
            httpx.HTTPError: unavailable,
        },
    )
    app.state.notification_service = service
    app.state.workload_authenticator = WorkloadPrincipalAuthenticator(principals)
    app.state.notification_workers = []
    app.include_router(router)
    return app
