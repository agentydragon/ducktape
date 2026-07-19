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
import os
import secrets
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from haku.console.config import NodeDaemonsConfig
from haku.console.database_schema import NodeDaemonExecution, NodeDaemonPresence
from haku.console.node_daemon_models import NodeDaemonExecutionStatus, NodeDaemonPresenceStatus

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
    status: NodeDaemonPresenceStatus
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


class NodeDaemonService:
    def __init__(self, sessions: sessionmaker[Session], config: NodeDaemonsConfig) -> None:
        self._sessions = sessions
        self.config = config
        credentials: list[_DaemonCredential] = []
        for daemon_id, definition in config.daemons.items():
            token = os.environ.get(definition.token_env_var)
            if not token:
                raise RuntimeError(f"missing node daemon token env var {definition.token_env_var} for {daemon_id}")
            credentials.append(_DaemonCredential(daemon_id, _fingerprint(token)))
        if len({credential.fingerprint for credential in credentials}) != len(credentials):
            raise RuntimeError("duplicate node daemon bearer tokens")
        self._credentials = tuple(credentials)
        self._work_available = asyncio.Event()

    def authenticate(self, authorization: str | None) -> str:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="node daemon bearer required")
        presented = _fingerprint(authorization.removeprefix("Bearer "))
        for credential in self._credentials:
            if secrets.compare_digest(presented, credential.fingerprint):
                return credential.daemon_id
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid node daemon bearer")

    def heartbeat(self, daemon_id: str, request: HeartbeatRequest) -> HeartbeatResponse:
        definition = self.config.daemons[daemon_id]
        if not set(request.backends).issubset(definition.backends):
            raise HTTPException(status_code=409, detail="daemon advertised an unconfigured backend")
        now = _now()
        with self._sessions.begin() as session:
            presence = session.get(NodeDaemonPresence, daemon_id, with_for_update=True)
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
                    session.query(NodeDaemonExecution).filter(
                        NodeDaemonExecution.daemon_id == daemon_id,
                        NodeDaemonExecution.status == NodeDaemonExecutionStatus.CLAIMED,
                    ).update(
                        {
                            NodeDaemonExecution.status: NodeDaemonExecutionStatus.FAILED,
                            NodeDaemonExecution.error: "daemon process replaced; execution outcome unknown",
                            NodeDaemonExecution.completed_at: now,
                        }
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

    def _expire(self, session: Session, now: datetime.datetime) -> None:
        session.query(NodeDaemonExecution).filter(
            NodeDaemonExecution.status == NodeDaemonExecutionStatus.PENDING,
            NodeDaemonExecution.dispatch_expires_at <= now,
        ).update(
            {
                NodeDaemonExecution.status: NodeDaemonExecutionStatus.FAILED,
                NodeDaemonExecution.error: "connected daemon did not claim execution before the dispatch deadline",
                NodeDaemonExecution.completed_at: now,
            }
        )
        session.query(NodeDaemonExecution).filter(
            NodeDaemonExecution.status == NodeDaemonExecutionStatus.CLAIMED, NodeDaemonExecution.lease_expires_at <= now
        ).update(
            {
                NodeDaemonExecution.status: NodeDaemonExecutionStatus.FAILED,
                NodeDaemonExecution.error: "daemon lease expired; execution outcome unknown",
                NodeDaemonExecution.completed_at: now,
            }
        )

    def enqueue(self, *, daemon_id: str, backend: str, payload: dict[str, Any]) -> UUID:
        now = _now()
        connected_after = now - datetime.timedelta(seconds=self.config.connected_after_seconds)
        with self._sessions.begin() as session:
            self._expire(session, now)
            presence = session.get(NodeDaemonPresence, daemon_id)
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
                    status=NodeDaemonExecutionStatus.PENDING,
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

    def _claim_once(self, daemon_id: str, instance_id: UUID) -> ClaimedExecution | None:
        now = _now()
        with self._sessions.begin() as session:
            self._expire(session, now)
            presence = session.get(NodeDaemonPresence, daemon_id)
            if presence is None or presence.instance_id != instance_id:
                raise HTTPException(status_code=409, detail="daemon instance is not current; heartbeat first")
            active = session.scalar(
                select(func.count())
                .select_from(NodeDaemonExecution)
                .where(
                    NodeDaemonExecution.daemon_id == daemon_id,
                    NodeDaemonExecution.status == NodeDaemonExecutionStatus.CLAIMED,
                )
            )
            if cast(int, active) >= presence.capacity:
                return None
            execution = session.scalars(
                select(NodeDaemonExecution)
                .where(
                    NodeDaemonExecution.daemon_id == daemon_id,
                    NodeDaemonExecution.status == NodeDaemonExecutionStatus.PENDING,
                )
                .order_by(NodeDaemonExecution.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).first()
            if execution is None:
                return None
            lease_token = secrets.token_urlsafe(32)
            lease_expires_at = now + datetime.timedelta(seconds=self.config.lease_seconds)
            execution.status = NodeDaemonExecutionStatus.CLAIMED
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
            claimed = await asyncio.to_thread(self._claim_once, daemon_id, request.instance_id)
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

    def _claimed(
        self, session: Session, daemon_id: str, execution_id: UUID, request: LeaseRequest
    ) -> NodeDaemonExecution:
        execution = self._validate_lease_identity(
            session.get(NodeDaemonExecution, execution_id, with_for_update=True), daemon_id, request
        )
        if execution.status != NodeDaemonExecutionStatus.CLAIMED:
            raise HTTPException(status_code=409, detail=f"execution is {execution.status}")
        if execution.lease_expires_at is None or execution.lease_expires_at <= _now():
            raise HTTPException(status_code=409, detail="execution lease expired")
        return execution

    def renew(self, daemon_id: str, execution_id: UUID, request: LeaseRequest) -> LeaseResponse:
        with self._sessions.begin() as session:
            execution = self._claimed(session, daemon_id, execution_id, request)
            execution.lease_expires_at = _now() + datetime.timedelta(seconds=self.config.lease_seconds)
            return LeaseResponse(lease_expires_at=execution.lease_expires_at)

    def finish(self, daemon_id: str, execution_id: UUID, request: ExecutionResultRequest) -> None:
        with self._sessions.begin() as session:
            existing = session.get(NodeDaemonExecution, execution_id, with_for_update=True)
            if existing is not None and existing.status in {
                NodeDaemonExecutionStatus.SUCCEEDED,
                NodeDaemonExecutionStatus.FAILED,
            }:
                self._validate_lease_identity(existing, daemon_id, request)
                if (
                    existing.status == NodeDaemonExecutionStatus(request.outcome)
                    and existing.result_json == request.result
                    and existing.error == request.error
                ):
                    return
                raise HTTPException(status_code=409, detail="execution already completed with a different outcome")
            execution = self._claimed(session, daemon_id, execution_id, request)
            execution.status = NodeDaemonExecutionStatus(request.outcome)
            execution.result_json = request.result
            execution.error = request.error
            execution.completed_at = _now()

    async def wait(self, execution_id: UUID) -> dict[str, Any]:
        while True:

            def read() -> tuple[str, dict[str, Any] | None, str | None]:
                with self._sessions.begin() as session:
                    self._expire(session, _now())
                    row = session.get(NodeDaemonExecution, execution_id)
                    if row is None:
                        raise RuntimeError("node daemon execution disappeared")
                    return row.status, row.result_json, row.error

            state, result, error = await asyncio.to_thread(read)
            if state == NodeDaemonExecutionStatus.SUCCEEDED:
                assert result is not None
                return result
            if state == NodeDaemonExecutionStatus.FAILED:
                raise RuntimeError(error or "node daemon execution failed")
            await asyncio.sleep(0.25)

    def statuses(self) -> DaemonStatusResponse:
        now = _now()
        connected_cutoff = now - datetime.timedelta(seconds=self.config.connected_after_seconds)
        offline_cutoff = now - datetime.timedelta(seconds=self.config.offline_after_seconds)
        with self._sessions.begin() as session:
            self._expire(session, now)
            presences = {row.daemon_id: row for row in session.scalars(select(NodeDaemonPresence)).all()}
            active = {
                row.daemon_id: row.execution_id
                for row in session.scalars(
                    select(NodeDaemonExecution).where(NodeDaemonExecution.status == NodeDaemonExecutionStatus.CLAIMED)
                ).all()
            }
        result: list[DaemonStatus] = []
        for daemon_id, definition in self.config.daemons.items():
            presence = presences.get(daemon_id)
            if presence is None or presence.last_heartbeat_at < offline_cutoff:
                state = NodeDaemonPresenceStatus.OFFLINE
            elif presence.last_heartbeat_at < connected_cutoff:
                state = NodeDaemonPresenceStatus.STALE
            elif daemon_id in active:
                state = NodeDaemonPresenceStatus.BUSY
            else:
                state = NodeDaemonPresenceStatus.CONNECTED
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


def _service(request: Request) -> NodeDaemonService:
    service = cast(NodeDaemonService | None, request.app.state.node_daemon_service)
    if service is None:
        raise HTTPException(status_code=503, detail="node daemons are not configured")
    return service


def _daemon(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    return _service(request).authenticate(authorization)


@machine_router.post("/heartbeat")
def heartbeat(body: HeartbeatRequest, request: Request) -> HeartbeatResponse:
    service = _service(request)
    return service.heartbeat(_daemon(request, request.headers.get("authorization")), body)


@machine_router.post("/work/claim", response_model=ClaimedExecution, responses={204: {"description": "No work"}})
async def claim(body: ClaimRequest, request: Request) -> ClaimedExecution | Response:
    service = _service(request)
    daemon_id = _daemon(request, request.headers.get("authorization"))
    execution = await service.claim(daemon_id, body)
    return execution if execution is not None else Response(status_code=204)


@machine_router.post("/executions/{execution_id}/heartbeat")
def renew(execution_id: UUID, body: LeaseRequest, request: Request) -> LeaseResponse:
    service = _service(request)
    return service.renew(_daemon(request, request.headers.get("authorization")), execution_id, body)


@machine_router.post("/executions/{execution_id}/result", status_code=204)
def finish(execution_id: UUID, body: ExecutionResultRequest, request: Request) -> Response:
    service = _service(request)
    service.finish(_daemon(request, request.headers.get("authorization")), execution_id, body)
    return Response(status_code=204)
