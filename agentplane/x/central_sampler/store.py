"""Canonical Sessions, command admission and the transactional event outbox."""

import json
from datetime import UTC, datetime
from typing import Any, cast, override
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, LargeBinary, func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from agentplane.postgres.wakeups import notify
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2
from agentplane.x.central_sampler import sampler_pb2
from agentplane.x.central_sampler.evidence import BOUNDARY_BYTES, display_text
from agentplane.x.postgres_codex import store

OUTBOX_CHANNEL = "central_sampler_outbox"


class Session(store.Base):
    __tablename__ = "sampler_session"

    id: Mapped[str] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey(store.Conversation.id), unique=True)
    account_uid: Mapped[str]
    configuration: Mapped[bytes] = mapped_column(LargeBinary)
    model: Mapped[str]
    effort: Mapped[str]
    source_id: Mapped[str]
    cursor: Mapped[int] = mapped_column(default=0)
    active_turn: Mapped[str | None]
    active_command: Mapped[str | None]
    workspace_prepared: Mapped[bool] = mapped_column(default=False)
    setup_state: Mapped[int] = mapped_column(default=runner_pb2.SETUP_STATE_NOT_REQUIRED)
    setup_started: Mapped[bool] = mapped_column(default=False)
    blocked: Mapped[bool] = mapped_column(default=False)


class Command(store.Base):
    __tablename__ = "sampler_command"

    session_id: Mapped[str] = mapped_column(ForeignKey(Session.id), primary_key=True)
    id: Mapped[str] = mapped_column(primary_key=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    admission: Mapped[int]
    outcome: Mapped[int] = mapped_column(default=sampler_pb2.COMMAND_OUTCOME_ACCEPTED)
    failure_reason: Mapped[str | None]


class Event(store.Base):
    __tablename__ = "sampler_event"

    session_id: Mapped[str] = mapped_column(ForeignKey(Session.id), primary_key=True)
    sequence: Mapped[int] = mapped_column(primary_key=True)
    entry: Mapped[bytes] = mapped_column(LargeBinary)


class ExportCheckpoint(store.Base):
    __tablename__ = "sampler_export_checkpoint"

    session_id: Mapped[str] = mapped_column(ForeignKey(Session.id), primary_key=True)
    destination: Mapped[str] = mapped_column(primary_key=True)
    cursor: Mapped[int]


class ConflictError(Exception):
    pass


class MissingError(Exception):
    pass


class ForbiddenError(Exception):
    pass


class PreconditionError(Exception):
    pass


class Store(store.Store):
    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine)
        self.database_url = engine.url

    @staticmethod
    async def append(db: AsyncSession, session: Session, event: event_pb2.Event) -> event_log_pb2.EventEntry:
        session.cursor += 1
        event.at.FromDatetime(datetime.now(UTC))
        entry = event_log_pb2.EventEntry(
            cursor=session.cursor,
            origin=event_log_pb2.EventOrigin(source_id=session.source_id, sequence=session.cursor),
            event=event,
        )
        db.add(Event(session_id=session.id, sequence=session.cursor, entry=entry.SerializeToString()))
        await notify(db, OUTBOX_CHANNEL)
        return entry

    @staticmethod
    async def lock_session(db: AsyncSession, session_id: str) -> tuple[store.Conversation, Session]:
        # All writers lock the conversation first: runtime evidence uses this same order.
        conversation = await db.scalar(
            select(store.Conversation)
            .join(Session, Session.conversation_id == store.Conversation.id)
            .where(Session.id == session_id)
            .with_for_update(of=store.Conversation)
        )
        if conversation is None:
            raise MissingError
        session = (await db.scalars(select(Session).where(Session.id == session_id).with_for_update())).one()
        return conversation, session

    async def open_session(self, request: sampler_pb2.OpenSessionRequest, account_uid: str) -> Session:
        # Advisory lock also serializes competing first creations, before a row exists.
        async with self.sessions.begin() as db:
            await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(request.session_id))))
            existing = await db.get(Session, request.session_id)
            wire = request.SerializeToString(deterministic=True)
            if existing is not None:
                if existing.account_uid != account_uid:
                    raise ForbiddenError
                if existing.configuration != wire:
                    raise ConflictError
                return existing
            conversation = store.Conversation(id=uuid4(), instructions=request.spec.instructions or None, items=[])
            db.add(conversation)
            await db.flush()
            session = Session(
                id=request.session_id,
                conversation_id=conversation.id,
                account_uid=account_uid,
                configuration=wire,
                model=request.spec.model,
                effort=request.spec.reasoning_effort,
                source_id=str(uuid4()),
                cursor=0,
                blocked=False,
                setup_state=runner_pb2.SETUP_STATE_RUNNING
                if request.setup_script
                else runner_pb2.SETUP_STATE_NOT_REQUIRED,
            )
            db.add(session)
            await db.flush()
            await self.append(
                db,
                session,
                event_pb2.Event(
                    execution_progress=event_pb2.ExecutionProgress(
                        operation_id=session.id, kind="session", phase="opened", payload_json="{}"
                    )
                ),
            )
            if not request.setup_script:
                await self.append(db, session, event_pb2.Event(harness_started=event_pb2.HarnessStarted()))
            return session

    async def get_session(self, session_id: str, account_uid: str | None = None) -> Session:
        async with self.sessions() as db:
            session = await db.get(Session, session_id)
            if session is None:
                raise MissingError
            if account_uid is not None and session.account_uid != account_uid:
                raise ForbiddenError
            return session

    async def recoverable_sessions(self, *, exclude: set[str], limit: int) -> list[str]:
        async with self.sessions() as db:
            return list(
                await db.scalars(
                    select(Session.id)
                    .join(store.Conversation, store.Conversation.id == Session.conversation_id)
                    .where(
                        Session.blocked.is_(False),
                        or_(
                            store.Conversation.owner.is_(None),
                            store.Conversation.lease_until.is_(None),
                            store.Conversation.lease_until <= func.clock_timestamp(),
                        ),
                        Session.id.not_in(exclude),
                        or_(
                            Session.setup_state == runner_pb2.SETUP_STATE_RUNNING,
                            select(Command.id)
                            .where(
                                Command.session_id == Session.id,
                                Command.outcome == sampler_pb2.COMMAND_OUTCOME_ACCEPTED,
                            )
                            .exists(),
                            select(store.LiveCell.id)
                            .where(
                                store.LiveCell.conversation_id == Session.conversation_id,
                                store.LiveCell.status == "live",
                            )
                            .exists(),
                            select(store.RemoteProcess.id)
                            .where(
                                store.RemoteProcess.conversation_id == Session.conversation_id,
                                store.RemoteProcess.status.not_in({"exited", "exited_unread"}),
                            )
                            .exists(),
                        ),
                    )
                    .order_by(Session.id)
                    .limit(limit)
                )
            )

    async def list_sessions(self, account_uid: str | None = None) -> list[Session]:
        async with self.sessions() as db:
            query = select(Session).order_by(Session.id)
            if account_uid is not None:
                query = query.where(Session.account_uid == account_uid)
            return list(await db.scalars(query))

    @staticmethod
    def describe(session: Session) -> sampler_pb2.GetSessionResponse:
        config = sampler_pb2.OpenSessionRequest.FromString(session.configuration)
        spec = runner_pb2.SessionSpec()
        spec.CopyFrom(config.spec)
        spec.model, spec.reasoning_effort = session.model, session.effort
        return sampler_pb2.GetSessionResponse(
            attached=runner_pb2.Attached(
                session_id=session.id,
                spec=spec,
                last_cursor=session.cursor,
                active_turn_id=session.active_turn or "",
                harness_state=runner_pb2.HARNESS_STATE_RUNNING
                if not session.blocked and session.setup_state != runner_pb2.SETUP_STATE_RUNNING
                else runner_pb2.HARNESS_STATE_STOPPED,
                setup_state=cast(runner_pb2.SetupState, session.setup_state),
                launch_outcome=runner_pb2.LAUNCH_OUTCOME_PENDING
                if session.setup_state == runner_pb2.SETUP_STATE_RUNNING
                else runner_pb2.LAUNCH_OUTCOME_FAILED
                if session.setup_state in {runner_pb2.SETUP_STATE_FAILED, runner_pb2.SETUP_STATE_INTERRUPTED}
                else runner_pb2.LAUNCH_OUTCOME_STARTED,
                incarnation_uid=config.expected_incarnation_uid,
            ),
            executor=config.executor,
            expected_incarnation_uid=config.expected_incarnation_uid,
            blocked=session.blocked,
        )

    async def admit(self, session_id: str, command: command_pb2.Command, account_uid: str) -> event_log_pb2.EventEntry:
        wire = command.SerializeToString(deterministic=True)
        async with self.sessions.begin() as db:
            conversation, session = await self.lock_session(db, session_id)
            if session.account_uid != account_uid:
                raise ForbiddenError
            existing = await db.get(Command, (session_id, command.command_id))
            if existing is not None:
                if existing.payload != wire:
                    raise ConflictError
                event = await db.get(Event, (session_id, existing.admission))
                assert event is not None
                return event_log_pb2.EventEntry.FromString(event.entry)
            if session.blocked:
                raise PreconditionError
            if command.HasField("interrupt_turn"):
                if session.active_turn != command.interrupt_turn.turn_id or conversation.owner is None:
                    raise PreconditionError
                db.add(
                    store.InterruptRequest(
                        id=uuid4(),
                        conversation_id=conversation.id,
                        generation=conversation.generation,
                        target_turn=session.active_turn,
                    )
                )
            admission = await self.append(
                db, session, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))
            )
            db.add(Command(session_id=session_id, id=command.command_id, payload=wire, admission=admission.cursor))
            return admission

    async def get_command(self, session_id: str, command_id: str) -> sampler_pb2.GetCommandResponse:
        async with self.sessions() as db:
            command = await db.get(Command, (session_id, command_id))
            if command is None:
                raise MissingError
            event = await db.get(Event, (session_id, command.admission))
            assert event is not None
            return sampler_pb2.GetCommandResponse(
                admission=event_log_pb2.EventEntry.FromString(event.entry),
                outcome=cast(sampler_pb2.CommandOutcome, command.outcome),
                failure_reason=command.failure_reason or "",
            )

    async def events(self, session_id: str, after: int, *, limit: int = 256) -> list[event_log_pb2.EventEntry]:
        async with self.sessions() as db:
            return [
                event_log_pb2.EventEntry.FromString(event.entry)
                for event in await db.scalars(
                    select(Event)
                    .where(Event.session_id == session_id, Event.sequence > after)
                    .order_by(Event.sequence)
                    .limit(limit)
                )
            ]

    @override
    async def provider_headers(self, owner: store.Owner) -> dict[str, str]:
        return {"x-agentplane-session-id": (await self.provider_metadata(owner))["session_id"]}

    @override
    async def provider_metadata(self, owner: store.Owner) -> dict[str, str]:
        async with self.sessions() as db:
            session = (await db.scalars(select(Session).where(Session.conversation_id == owner.conversation_id))).one()
            return {"session_id": session.id, "thread_id": session.id, "turn_id": session.active_turn or session.id}

    @override
    async def record_evidence(
        self, db: AsyncSession, owner: store.Owner, operation_id: str, kind: str, phase: str, payload: dict[str, Any]
    ) -> None:
        session = (
            await db.scalars(select(Session).where(Session.conversation_id == owner.conversation_id).with_for_update())
        ).one()
        conversation = await db.get(store.Conversation, owner.conversation_id)
        assert conversation is not None
        parent = str(
            payload.get(
                "cell_id",
                conversation.pending["id"]
                if conversation.pending is not None and kind in {"javascript", "executor", "tool"}
                else session.active_turn or "",
            )
        )
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        record_id = str(uuid4())
        sources = []
        count = max(1, (len(encoded) + BOUNDARY_BYTES - 1) // BOUNDARY_BYTES)
        for index in range(count):
            evidence = await self.append(
                db,
                session,
                event_pb2.Event(
                    boundary_record=event_pb2.BoundaryRecord(
                        boundary=kind,
                        direction=phase,
                        payload=encoded[index * BOUNDARY_BYTES : (index + 1) * BOUNDARY_BYTES],
                        media_type="application/json",
                        correlation_id=operation_id,
                        parent_id=parent,
                        record_id=record_id,
                        fragment_index=index,
                        fragment_count=count,
                    )
                ),
            )
            sources.append(evidence.origin.sequence)
        await self.append(
            db,
            session,
            event_pb2.Event(
                source_sequences=sources,
                execution_progress=event_pb2.ExecutionProgress(
                    operation_id=operation_id, parent_id=parent, kind=kind, phase=phase, payload_json="{}"
                ),
            ),
        )
        if kind == "compaction" and phase == "result" and session.active_command is not None:
            command = await db.get(Command, (session.id, session.active_command))
            assert command is not None
            if command_pb2.Command.FromString(command.payload).HasField("compact"):
                command.outcome = sampler_pb2.COMMAND_OUTCOME_APPLIED
                session.active_command = None
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        source_sequences=sources,
                        execution_progress=event_pb2.ExecutionProgress(
                            operation_id=command.id, kind="command", phase="completed", payload_json="{}"
                        ),
                    ),
                )
        if phase == "rejected":
            await self.settle_failure(
                db, conversation, session, session.active_command, store.KnownFailureError(payload)
            )
        if kind == "provider":
            await self.provider_observation(db, session, sources, payload)
        elif kind == "cell" and phase == "result" and payload.get("type") == "custom_tool_call_output":
            await self.append(
                db,
                session,
                event_pb2.Event(
                    source_sequences=sources,
                    item_completed=event_pb2.ItemCompleted(
                        item_id=payload["call_id"],
                        tool=event_pb2.ToolResult(output=display_text(json.dumps(payload["output"])), succeeded=True),
                    ),
                ),
            )

    async def provider_observation(
        self, db: AsyncSession, session: Session, sources: list[int], payload: dict[str, Any]
    ) -> None:
        event_type = payload.get("type")
        item_id = payload.get("item_id", "")
        if event_type in {
            "response.output_text.delta",
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            await self.append(
                db,
                session,
                event_pb2.Event(
                    source_sequences=sources,
                    text_delta=event_pb2.TextDelta(item_id=item_id, text=display_text(payload["delta"])),
                ),
            )
        elif event_type in {"response.output_item.added", "response.output_item.done"}:
            item = payload["item"]
            kinds = {
                "message": event_pb2.ITEM_KIND_ASSISTANT_TEXT,
                "reasoning": event_pb2.ITEM_KIND_REASONING,
                "custom_tool_call": event_pb2.ITEM_KIND_TOOL_CALL,
            }
            if item["type"] not in kinds:
                return
            item_id = item.get("call_id", item["id"])
            if event_type == "response.output_item.added":
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        source_sequences=sources,
                        item_started=event_pb2.ItemStarted(
                            item_id=item_id, kind=kinds[item["type"]], tool_name=item.get("name", "")
                        ),
                    ),
                )
            elif item["type"] == "custom_tool_call":
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        source_sequences=sources,
                        tool_arguments=event_pb2.ToolArguments(
                            item_id=item_id,
                            arguments_json=json.dumps({"source": display_text(item["input"])}, ensure_ascii=False),
                        ),
                    ),
                )
            else:
                parts = item.get("summary", []) if item["type"] == "reasoning" else item.get("content", [])
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        source_sequences=sources,
                        item_completed=event_pb2.ItemCompleted(
                            item_id=item_id, text=display_text("".join(part.get("text", "") for part in parts))
                        ),
                    ),
                )

    async def prepared_workspace(self, owner: store.Owner, session_id: str) -> None:
        async with self.sessions.begin() as db:
            await self._lock(db, owner)
            _, session = await self.lock_session(db, session_id)
            session.workspace_prepared = True
            await self.append(
                db,
                session,
                event_pb2.Event(
                    execution_progress=event_pb2.ExecutionProgress(
                        operation_id=session_id, kind="workspace", phase="prepared", payload_json="{}"
                    )
                ),
            )

    async def start_setup(self, owner: store.Owner, session_id: str) -> bool:
        async with self.sessions.begin() as db:
            conversation = await self._lock(db, owner)
            _, session = await self.lock_session(db, session_id)
            if session.setup_started:
                # Ownership was lost after the one attempt began. Even a lost completion
                # acknowledgement cannot authorize another attempt at workspace mutations.
                await self.settle_setup(db, conversation, session, None)
                return False
            session.setup_started = True
            await self.append(db, session, event_pb2.Event(setup_started=event_pb2.SetupStarted()))
            return True

    async def setup_output(self, owner: store.Owner, session_id: str, stream: str, data: bytes) -> None:
        if stream not in {"stdout", "stderr"}:
            raise ValueError("unknown setup output stream")
        async with self.sessions.begin() as db:
            await self._lock(db, owner)
            _, session = await self.lock_session(db, session_id)
            await self.append(db, session, event_pb2.Event(setup_output=event_pb2.SetupOutput(**{stream: data})))

    async def settle_setup(
        self, db: AsyncSession, conversation: store.Conversation, session: Session, exit_code: int | None
    ) -> None:
        if session.setup_state != runner_pb2.SETUP_STATE_RUNNING:
            raise PreconditionError
        if exit_code is None:
            session.setup_state = runner_pb2.SETUP_STATE_INTERRUPTED
            await self.append(db, session, event_pb2.Event(setup_interrupted=event_pb2.SetupInterrupted()))
        else:
            session.setup_state = runner_pb2.SETUP_STATE_SUCCEEDED if exit_code == 0 else runner_pb2.SETUP_STATE_FAILED
            await self.append(db, session, event_pb2.Event(setup_finished=event_pb2.SetupFinished(exit_code=exit_code)))
        if exit_code == 0:
            await self.append(db, session, event_pb2.Event(harness_started=event_pb2.HarnessStarted()))
        else:
            session.blocked = True
            conversation.stopped = True
            await self.block_queue(db, session, reason="session setup failed or was interrupted; create a new session")

    async def finish_setup(self, owner: store.Owner, session_id: str, exit_code: int | None) -> None:
        async with self.sessions.begin() as db:
            conversation = await self._lock(db, owner)
            _, session = await self.lock_session(db, session_id)
            await self.settle_setup(db, conversation, session, exit_code)

    async def check_recovery(self, owner: store.Owner) -> None:
        async with self.sessions.begin() as db:
            await self._lock(db, owner)
            unfinished = await db.scalar(
                select(store.Intent.id)
                .where(
                    store.Intent.conversation_id == owner.conversation_id,
                    store.Intent.generation != owner.generation,
                    store.Intent.result.is_(None),
                )
                .limit(1)
            )
            if unfinished is not None:
                raise store.UnknownEffectError("earlier owner left an unresolved effect")

    async def next_command(self, owner: store.Owner, session_id: str) -> Command | None:
        async with self.sessions.begin() as db:
            conversation = await self._lock(db, owner)
            _, session = await self.lock_session(db, session_id)
            if session.blocked:
                raise PreconditionError
            if session.active_command is not None:
                for candidate in await db.scalars(
                    select(Command).where(
                        Command.session_id == session_id, Command.outcome == sampler_pb2.COMMAND_OUTCOME_ACCEPTED
                    )
                ):
                    payload = command_pb2.Command.FromString(candidate.payload)
                    if payload.HasField("interrupt_turn") and payload.interrupt_turn.turn_id == session.active_turn:
                        pending = await db.scalar(
                            select(store.InterruptRequest.id).where(
                                store.InterruptRequest.conversation_id == owner.conversation_id,
                                store.InterruptRequest.generation == owner.generation,
                                store.InterruptRequest.target_turn == session.active_turn,
                                store.InterruptRequest.result.is_(None),
                            )
                        )
                        if pending is None:
                            db.add(
                                store.InterruptRequest(
                                    id=uuid4(),
                                    conversation_id=owner.conversation_id,
                                    generation=owner.generation,
                                    target_turn=session.active_turn,
                                )
                            )
                        break
                return await db.get(Command, (session_id, session.active_command))
            command = await db.scalar(
                select(Command)
                .where(Command.session_id == session_id, Command.outcome == sampler_pb2.COMMAND_OUTCOME_ACCEPTED)
                .order_by(Command.admission)
                .limit(1)
            )
            if command is None:
                return None
            payload = command_pb2.Command.FromString(command.payload)
            operation = payload.WhichOneof("operation")
            if operation == "submit_input":
                if conversation.pending is not None:
                    raise store.UnknownEffectError("unresolved intent blocks queued input")
                item = {"type": "message", "role": "user", "content": payload.submit_input.text}
                conversation.items = [*conversation.items, item]
                if conversation.context_items is not None:
                    conversation.context_items = [*conversation.context_items, item]
                conversation.stopped = False
                session.active_turn = command.id
                session.active_command = command.id
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                            harness_message_id=command.id,
                            text=payload.submit_input.text,
                            origin_command_ids=[command.id],
                            turn_id=command.id,
                        )
                    ),
                )
                await self.append(
                    db,
                    session,
                    event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id=command.id, model=session.model)),
                )
            elif operation == "change_model":
                previous = session.model
                session.model = payload.change_model.model
                command.outcome = sampler_pb2.COMMAND_OUTCOME_APPLIED
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        model_changed=event_pb2.ModelChanged(
                            command_id=command.id, previous_model=previous, model=session.model
                        )
                    ),
                )
            elif operation == "change_reasoning_effort":
                previous = session.effort
                session.effort = payload.change_reasoning_effort.effort
                command.outcome = sampler_pb2.COMMAND_OUTCOME_APPLIED
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        reasoning_effort_changed=event_pb2.ReasoningEffortChanged(
                            command_id=command.id, previous_effort=previous, effort=session.effort
                        )
                    ),
                )
            elif operation == "interrupt_turn":
                command.outcome = sampler_pb2.COMMAND_OUTCOME_APPLIED
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        command_noop=event_pb2.CommandNoop(command_id=command.id, reason="target turn has ended")
                    ),
                )
            elif operation == "compact":
                session.active_command = command.id
            else:
                raise ValueError("unsupported command")
            return command

    @staticmethod
    async def turn_interrupts(db: AsyncSession, owner: store.Owner) -> list[UUID]:
        return list(
            await db.scalars(
                select(store.InterruptRequest.id)
                .join(Session, Session.conversation_id == store.InterruptRequest.conversation_id)
                .where(
                    store.InterruptRequest.conversation_id == owner.conversation_id,
                    store.InterruptRequest.generation == owner.generation,
                    store.InterruptRequest.target_turn == Session.active_turn,
                    store.InterruptRequest.result.is_(None),
                )
            )
        )

    @override
    async def pending_interrupts(self, owner: store.Owner) -> list[UUID]:
        async with self.sessions.begin() as db:
            await self._lock(db, owner)
            return await self.turn_interrupts(db, owner)

    @staticmethod
    @override
    async def _check_interrupt(db: AsyncSession, owner: store.Owner) -> None:
        if await Store.turn_interrupts(db, owner):
            raise store.InterruptPendingError("interrupt already requested; next dispatch is blocked")

    @staticmethod
    async def settle_turn_interrupts(db: AsyncSession, session: Session, result: str) -> None:
        for request in await db.scalars(
            select(store.InterruptRequest).where(
                store.InterruptRequest.conversation_id == session.conversation_id,
                store.InterruptRequest.target_turn == session.active_turn,
                store.InterruptRequest.result.is_(None),
            )
        ):
            request.result = {"status": "not_applied", "reason": "target turn ended", "turn_result": result}

    async def finish_command(self, owner: store.Owner, session_id: str, command_id: str, result: str) -> None:
        async with self.sessions.begin() as db:
            await self._lock(db, owner)
            _, session = await self.lock_session(db, session_id)
            command = await db.get(Command, (session_id, command_id))
            assert command is not None
            if command.outcome != sampler_pb2.COMMAND_OUTCOME_ACCEPTED:
                return
            session.active_command = None
            command.outcome = (
                sampler_pb2.COMMAND_OUTCOME_UNKNOWN
                if result == "effects_unknown"
                else sampler_pb2.COMMAND_OUTCOME_APPLIED
            )
            session.blocked = result == "effects_unknown"
            if session.active_turn == command_id:
                await self.settle_turn_interrupts(db, session, result)
                interrupts = list(
                    await db.scalars(
                        select(Command).where(
                            Command.session_id == session_id, Command.outcome == sampler_pb2.COMMAND_OUTCOME_ACCEPTED
                        )
                    )
                )
                interrupt_id = ""
                for candidate in interrupts:
                    payload = command_pb2.Command.FromString(candidate.payload)
                    if payload.HasField("interrupt_turn") and payload.interrupt_turn.turn_id == command_id:
                        candidate.outcome = command.outcome
                        if result == "done":
                            await self.append(
                                db,
                                session,
                                event_pb2.Event(
                                    command_noop=event_pb2.CommandNoop(
                                        command_id=candidate.id, reason="turn completed before interrupt was applied"
                                    )
                                ),
                            )
                        else:
                            interrupt_id = candidate.id
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        turn_completed=event_pb2.TurnCompleted(
                            turn_id=command_id,
                            status=event_pb2.TURN_STATUS_COMPLETED
                            if result == "done"
                            else event_pb2.TURN_STATUS_INTERRUPTED,
                            interrupted_by_command_id=interrupt_id,
                        )
                    ),
                )
                session.active_turn = None
            else:
                await self.append(
                    db,
                    session,
                    event_pb2.Event(
                        execution_progress=event_pb2.ExecutionProgress(
                            operation_id=command_id, kind="command", phase="completed", payload_json="{}"
                        )
                    ),
                )

            if session.blocked:
                await self.block_queue(db, session)

    async def block_queue(
        self, db: AsyncSession, session: Session, *, reason: str = "session blocked by unresolved effects"
    ) -> None:
        for queued in await db.scalars(
            select(Command).where(
                Command.session_id == session.id, Command.outcome == sampler_pb2.COMMAND_OUTCOME_ACCEPTED
            )
        ):
            payload = command_pb2.Command.FromString(queued.payload)
            queued.outcome = (
                sampler_pb2.COMMAND_OUTCOME_UNKNOWN
                if payload.HasField("interrupt_turn")
                else sampler_pb2.COMMAND_OUTCOME_FAILED
            )
            queued.failure_reason = reason
            await self.append(
                db,
                session,
                event_pb2.Event(
                    command_failed=event_pb2.CommandFailed(command_id=queued.id, reason=queued.failure_reason)
                ),
            )

    async def settle_failure(
        self,
        db: AsyncSession,
        conversation: store.Conversation,
        session: Session,
        command_id: str | None,
        failure: store.KnownFailureError | None,
    ) -> None:
        command = await db.get(Command, (session.id, command_id)) if command_id is not None else None
        if command is not None and command.outcome != sampler_pb2.COMMAND_OUTCOME_ACCEPTED:
            return
        unresolved = await db.scalar(
            select(store.Intent.id)
            .where(store.Intent.conversation_id == conversation.id, store.Intent.result.is_(None))
            .limit(1)
        )
        live_cell = await db.scalar(
            select(store.LiveCell.id)
            .where(store.LiveCell.conversation_id == conversation.id, store.LiveCell.status == "live")
            .limit(1)
        )
        live_process = await db.scalar(
            select(store.RemoteProcess.id)
            .where(
                store.RemoteProcess.conversation_id == conversation.id,
                store.RemoteProcess.status.not_in({"exited", "exited_unread"}),
            )
            .limit(1)
        )
        session.blocked = (
            failure is None
            or conversation.pending is not None
            or unresolved is not None
            or live_cell is not None
            or live_process is not None
        )
        session.active_command = None
        reason = (
            "execution stopped; inspect durable boundary evidence" if failure is None else display_text(str(failure))
        )
        if command is not None:
            command.outcome = (
                sampler_pb2.COMMAND_OUTCOME_UNKNOWN if session.blocked else sampler_pb2.COMMAND_OUTCOME_FAILED
            )
            command.failure_reason = reason
            await self.append(
                db,
                session,
                event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id=command.id, reason=reason)),
            )
        conversation.stopped = True
        await self.append(
            db,
            session,
            event_pb2.Event(
                execution_progress=event_pb2.ExecutionProgress(
                    operation_id=session.id,
                    kind="session",
                    phase="blocked" if session.blocked else "failed",
                    payload_json=json.dumps({"reason": reason}),
                )
            ),
        )
        if session.active_turn:
            await self.settle_turn_interrupts(db, session, "failed")
            for candidate in await db.scalars(
                select(Command).where(
                    Command.session_id == session.id, Command.outcome == sampler_pb2.COMMAND_OUTCOME_ACCEPTED
                )
            ):
                payload = command_pb2.Command.FromString(candidate.payload)
                if payload.HasField("interrupt_turn") and payload.interrupt_turn.turn_id == session.active_turn:
                    candidate.outcome = sampler_pb2.COMMAND_OUTCOME_APPLIED
                    await self.append(
                        db,
                        session,
                        event_pb2.Event(
                            command_noop=event_pb2.CommandNoop(command_id=candidate.id, reason="target turn failed")
                        ),
                    )
            await self.append(
                db,
                session,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(
                        turn_id=session.active_turn, status=event_pb2.TURN_STATUS_FAILED, error=reason
                    )
                ),
            )
            session.active_turn = None
        if session.blocked:
            await self.block_queue(db, session)

    async def fail_execution(
        self,
        owner: store.Owner,
        session_id: str,
        command_id: str | None,
        *,
        failure: store.KnownFailureError | None = None,
    ) -> None:
        async with self.sessions.begin() as db:
            conversation = await self._lock(db, owner)
            _, session = await self.lock_session(db, session_id)
            await self.settle_failure(db, conversation, session, command_id, failure)


# Generated protobuf stubs require their direct typing dependency.
# gazelle:include_dep @pypi//protobuf
