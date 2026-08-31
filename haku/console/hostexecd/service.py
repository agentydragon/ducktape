"""Durable, daemon-initiated execution transport for node-local backends.

Node daemons authenticate with narrowly scoped bearer tokens, heartbeat into Postgres, long-poll
for work, renew a claim lease while executing, and submit an idempotent result. Postgres is the
authority across console replicas; process-local events only reduce claim latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import secrets
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.config import NodeDaemonsConfig
from haku.console.database_schema import NodeDaemonExecution, NodeDaemonPresence
from haku.console.hostexecd.models import ExecutionStatus, PresenceStatus

machine_router = APIRouter(prefix="/api/node-daemons/v1", tags=["node-daemons-machine"])
operator_router = APIRouter(prefix="/api/node-daemons", tags=["node-daemons"])


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _fingerprint(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class HeartbeatRequest(BaseModel):
    instance_id: UUID
    version: str = Field(min_length=1, max_length=200)
    backends: list[str] = Field(min_length=1)
    capacity: int = Field(default=1, ge=1, le=32)


class HeartbeatResponse(BaseModel):
    heartbeat_interval_seconds: int
    lease_seconds: int


class ClaimRequest(BaseModel):
    instance_id: UUID
    wait_seconds: int | None = Field(default=None, ge=0, le=25)


class ClaimedExecution(BaseModel):
    execution_id: UUID
    backend: str
    payload: dict[str, Any]
    lease_token: str
    lease_expires_at: datetime.datetime


class LeaseRequest(BaseModel):
    instance_id: UUID
    lease_token: str


class LeaseResponse(BaseModel):
    lease_expires_at: datetime.datetime


class ExecutionResultRequest(LeaseRequest):
    outcome: Literal["succeeded", "failed"]
    result: dict[str, Any] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> ExecutionResultRequest:
        if self.outcome == "succeeded" and (self.result is None or self.error is not None):
            raise ValueError("a succeeded execution requires result and no error")
        if self.outcome == "failed" and (not self.error or self.result is not None):
            raise ValueError("a failed execution requires error and no result")
        return self


class DaemonStatus(BaseModel):
    daemon_id: str
    display_name: str
    status: PresenceStatus
    last_heartbeat_at: datetime.datetime | None
    version: str | None
    backends: list[str]
    active_execution_id: UUID | None = None


class DaemonStatusResponse(BaseModel):
    daemons: list[DaemonStatus]


@dataclass(frozen=True, slots=True)
class _DaemonCredential:
    daemon_id: str
    fingerprint: bytes


class Service:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], config: NodeDaemonsConfig) -> None:
        self._sessions = sessions
        self.config = config
        credentials: list[_DaemonCredential] = []
        for daemon_id, definition in config.daemons.items():
            credentials.append(_DaemonCredential(daemon_id, _fingerprint(definition.token.get_secret_value())))
        if len({credential.fingerprint for credential in credentials}) != len(credentials):
            raise RuntimeError("duplicate node daemon bearer tokens")
        self._credentials = tuple(credentials)
        self._work_available = asyncio.Event()

    async def authenticate(self, authorization: str | None) -> str:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="node daemon bearer required")
        presented = _fingerprint(authorization.removeprefix("Bearer "))
        for credential in self._credentials:
            if secrets.compare_digest(presented, credential.fingerprint):
                return credential.daemon_id
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid node daemon bearer")

    async def heartbeat(self, daemon_id: str, request: HeartbeatRequest) -> HeartbeatResponse:
        definition = self.config.daemons[daemon_id]
        if not set(request.backends).issubset(definition.backends):
            raise HTTPException(status_code=409, detail="daemon advertised an unconfigured backend")
        now = _now()
        async with self._sessions.begin() as session:
            presence = await session.get(NodeDaemonPresence, daemon_id, with_for_update=True)
            if presence is None:
                presence = NodeDaemonPresence(
                    daemon_id=daemon_id,
                    instance_id=request.instance_id,
                    version=request.version,
                    backends_json=request.backends,
                    capacity=request.capacity,
                    connected_at=now,
                    last_heartbeat_at=now,
                )
                session.add(presence)
            else:
                if presence.instance_id != request.instance_id:
                    await session.execute(
                        update(NodeDaemonExecution)
                        .where(
                            NodeDaemonExecution.daemon_id == daemon_id,
                            NodeDaemonExecution.status == ExecutionStatus.CLAIMED,
                        )
                        .values(
                            status=ExecutionStatus.FAILED,
                            error="daemon process replaced; execution outcome unknown",
                            completed_at=now,
                        )
                    )
                    presence.connected_at = now
                presence.instance_id = request.instance_id
                presence.version = request.version
                presence.backends_json = request.backends
                presence.capacity = request.capacity
                presence.last_heartbeat_at = now
        return HeartbeatResponse(
            heartbeat_interval_seconds=self.config.heartbeat_interval_seconds, lease_seconds=self.config.lease_seconds
        )

    async def _expire(self, session: AsyncSession, now: datetime.datetime) -> None:
        await session.execute(
            update(NodeDaemonExecution)
            .where(
                NodeDaemonExecution.status == ExecutionStatus.PENDING, NodeDaemonExecution.dispatch_expires_at <= now
            )
            .values(
                status=ExecutionStatus.FAILED,
                error="connected daemon did not claim execution before the dispatch deadline",
                completed_at=now,
            )
        )
        await session.execute(
            update(NodeDaemonExecution)
            .where(NodeDaemonExecution.status == ExecutionStatus.CLAIMED, NodeDaemonExecution.lease_expires_at <= now)
            .values(
                status=ExecutionStatus.FAILED, error="daemon lease expired; execution outcome unknown", completed_at=now
            )
        )

    async def enqueue(self, *, daemon_id: str, backend: str, payload: dict[str, Any]) -> UUID:
        now = _now()
        connected_after = now - datetime.timedelta(seconds=self.config.connected_after_seconds)
        async with self._sessions.begin() as session:
            await self._expire(session, now)
            presence = await session.get(NodeDaemonPresence, daemon_id)
            if (
                presence is None
                or presence.last_heartbeat_at < connected_after
                or backend not in presence.backends_json
            ):
                raise RuntimeError(f"node daemon {daemon_id!r} is not connected with backend {backend!r}")
            execution_id = uuid4()
            session.add(
                NodeDaemonExecution(
                    execution_id=execution_id,
                    daemon_id=daemon_id,
                    backend=backend,
                    status=ExecutionStatus.PENDING,
                    payload_json=payload,
                    result_json=None,
                    error=None,
                    created_at=now,
                    dispatch_expires_at=now + datetime.timedelta(seconds=self.config.dispatch_timeout_seconds),
                    claimed_at=None,
                    instance_id=None,
                    lease_token_fingerprint=None,
                    lease_expires_at=None,
                    completed_at=None,
                )
            )
        self._work_available.set()
        return execution_id

    async def _claim_once(self, daemon_id: str, instance_id: UUID) -> ClaimedExecution | None:
        now = _now()
        async with self._sessions.begin() as session:
            await self._expire(session, now)
            presence = await session.get(NodeDaemonPresence, daemon_id)
            if presence is None or presence.instance_id != instance_id:
                raise HTTPException(status_code=409, detail="daemon instance is not current; heartbeat first")
            active = await session.scalar(
                select(func.count())
                .select_from(NodeDaemonExecution)
                .where(
                    NodeDaemonExecution.daemon_id == daemon_id, NodeDaemonExecution.status == ExecutionStatus.CLAIMED
                )
            )
            if cast(int, active) >= presence.capacity:
                return None
            result = await session.scalars(
                select(NodeDaemonExecution)
                .where(
                    NodeDaemonExecution.daemon_id == daemon_id, NodeDaemonExecution.status == ExecutionStatus.PENDING
                )
                .order_by(NodeDaemonExecution.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            execution = result.first()
            if execution is None:
                return None
            lease_token = secrets.token_urlsafe(32)
            lease_expires_at = now + datetime.timedelta(seconds=self.config.lease_seconds)
            execution.status = ExecutionStatus.CLAIMED
            execution.claimed_at = now
            execution.instance_id = instance_id
            execution.lease_token_fingerprint = _fingerprint(lease_token)
            execution.lease_expires_at = lease_expires_at
            return ClaimedExecution(
                execution_id=execution.execution_id,
                backend=execution.backend,
                payload=execution.payload_json,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
            )

    async def claim(self, daemon_id: str, request: ClaimRequest) -> ClaimedExecution | None:
        wait_seconds = request.wait_seconds if request.wait_seconds is not None else self.config.claim_wait_seconds
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            claimed = await self._claim_once(daemon_id, request.instance_id)
            if claimed is not None:
                return claimed
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            self._work_available.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._work_available.wait(), timeout=min(remaining, 0.5))

    def _validate_lease_identity(
        self, execution: NodeDaemonExecution | None, daemon_id: str, request: LeaseRequest
    ) -> NodeDaemonExecution:
        if execution is None or execution.daemon_id != daemon_id:
            raise HTTPException(status_code=404, detail="execution not found")
        if execution.instance_id != request.instance_id:
            raise HTTPException(status_code=409, detail="execution belongs to another daemon instance")
        if execution.lease_token_fingerprint is None or not secrets.compare_digest(
            execution.lease_token_fingerprint, _fingerprint(request.lease_token)
        ):
            raise HTTPException(status_code=403, detail="invalid execution lease")
        return execution

    async def _claimed(
        self, session: AsyncSession, daemon_id: str, execution_id: UUID, request: LeaseRequest
    ) -> NodeDaemonExecution:
        execution = self._validate_lease_identity(
            await session.get(NodeDaemonExecution, execution_id, with_for_update=True), daemon_id, request
        )
        if execution.status != ExecutionStatus.CLAIMED:
            raise HTTPException(status_code=409, detail=f"execution is {execution.status}")
        if execution.lease_expires_at is None or execution.lease_expires_at <= _now():
            raise HTTPException(status_code=409, detail="execution lease expired")
        return execution

    async def renew(self, daemon_id: str, execution_id: UUID, request: LeaseRequest) -> LeaseResponse:
        async with self._sessions.begin() as session:
            execution = await self._claimed(session, daemon_id, execution_id, request)
            execution.lease_expires_at = _now() + datetime.timedelta(seconds=self.config.lease_seconds)
            return LeaseResponse(lease_expires_at=execution.lease_expires_at)

    async def finish(self, daemon_id: str, execution_id: UUID, request: ExecutionResultRequest) -> None:
        async with self._sessions.begin() as session:
            existing = await session.get(NodeDaemonExecution, execution_id, with_for_update=True)
            if existing is not None and existing.status in {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED}:
                self._validate_lease_identity(existing, daemon_id, request)
                if (
                    existing.status == ExecutionStatus(request.outcome)
                    and existing.result_json == request.result
                    and existing.error == request.error
                ):
                    return
                raise HTTPException(status_code=409, detail="execution already completed with a different outcome")
            execution = await self._claimed(session, daemon_id, execution_id, request)
            execution.status = ExecutionStatus(request.outcome)
            execution.result_json = request.result
            execution.error = request.error
            execution.completed_at = _now()

    async def wait(self, execution_id: UUID) -> dict[str, Any]:
        while True:

            async def read() -> tuple[str, dict[str, Any] | None, str | None]:
                async with self._sessions.begin() as session:
                    await self._expire(session, _now())
                    row = await session.get(NodeDaemonExecution, execution_id)
                    if row is None:
                        raise RuntimeError("node daemon execution disappeared")
                    return row.status, row.result_json, row.error

            state, result, error = await read()
            if state == ExecutionStatus.SUCCEEDED:
                assert result is not None
                return result
            if state == ExecutionStatus.FAILED:
                raise RuntimeError(error or "node daemon execution failed")
            await asyncio.sleep(0.25)

    async def statuses(self) -> DaemonStatusResponse:
        now = _now()
        connected_cutoff = now - datetime.timedelta(seconds=self.config.connected_after_seconds)
        offline_cutoff = now - datetime.timedelta(seconds=self.config.offline_after_seconds)
        async with self._sessions.begin() as session:
            await self._expire(session, now)
            presences = {row.daemon_id: row for row in (await session.scalars(select(NodeDaemonPresence))).all()}
            active = {
                row.daemon_id: row.execution_id
                for row in (
                    await session.scalars(
                        select(NodeDaemonExecution).where(NodeDaemonExecution.status == ExecutionStatus.CLAIMED)
                    )
                ).all()
            }
        result: list[DaemonStatus] = []
        for daemon_id, definition in self.config.daemons.items():
            presence = presences.get(daemon_id)
            if presence is None or presence.last_heartbeat_at < offline_cutoff:
                state = PresenceStatus.OFFLINE
            elif presence.last_heartbeat_at < connected_cutoff:
                state = PresenceStatus.STALE
            elif daemon_id in active:
                state = PresenceStatus.BUSY
            else:
                state = PresenceStatus.CONNECTED
            result.append(
                DaemonStatus(
                    daemon_id=daemon_id,
                    display_name=definition.display_name,
                    status=state,
                    last_heartbeat_at=presence.last_heartbeat_at if presence else None,
                    version=presence.version if presence else None,
                    backends=presence.backends_json if presence else definition.backends,
                    active_execution_id=active.get(daemon_id),
                )
            )
        return DaemonStatusResponse(daemons=result)


def _service(request: Request) -> Service:
    service = cast(Service | None, request.app.state.hostexecd_service)
    if service is None:
        raise HTTPException(status_code=503, detail="node daemons are not configured")
    return service


ServiceDep = Annotated[Service, Depends(_service)]


async def _daemon(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    return await _service(request).authenticate(authorization)


DaemonIdDep = Annotated[str, Depends(_daemon)]


@machine_router.post("/heartbeat")
async def heartbeat(body: HeartbeatRequest, service: ServiceDep, daemon_id: DaemonIdDep) -> HeartbeatResponse:
    return await service.heartbeat(daemon_id, body)


@machine_router.post("/work/claim", response_model=ClaimedExecution, responses={204: {"description": "No work"}})
async def claim(body: ClaimRequest, service: ServiceDep, daemon_id: DaemonIdDep) -> ClaimedExecution | Response:
    execution = await service.claim(daemon_id, body)
    return execution if execution is not None else Response(status_code=204)


@machine_router.post("/executions/{execution_id}/heartbeat")
async def renew(execution_id: UUID, body: LeaseRequest, service: ServiceDep, daemon_id: DaemonIdDep) -> LeaseResponse:
    return await service.renew(daemon_id, execution_id, body)


@machine_router.post("/executions/{execution_id}/result", status_code=204)
async def finish(
    execution_id: UUID, body: ExecutionResultRequest, service: ServiceDep, daemon_id: DaemonIdDep
) -> Response:
    await service.finish(daemon_id, execution_id, body)
    return Response(status_code=204)
