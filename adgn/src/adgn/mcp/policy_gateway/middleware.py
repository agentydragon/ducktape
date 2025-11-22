from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import json
import logging
from typing import Any
import uuid
from uuid import UUID

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp import McpError, types as mtypes
from mcp.types import ErrorData

from adgn.agent.approvals import ApprovalHub
from adgn.agent.handler import AbortTurnDecision, ContinueDecision
from adgn.agent.persist import Decision, Persistence, ToolCall, ToolCallExecution, ToolCallRecord
from adgn.agent.policies.policy_types import ApprovalDecision, PolicyRequest
from adgn.agent.types import AgentID, ApprovalStatus
from adgn.mcp._shared.calltool import convert_fastmcp_result
from adgn.mcp._shared.constants import (
    POLICY_BACKEND_RESERVED_MISUSE_CODE,
    POLICY_BACKEND_RESERVED_MISUSE_MSG,
    POLICY_DENIED_ABORT_CODE,
    POLICY_DENIED_ABORT_MSG,
    POLICY_DENIED_CONTINUE_CODE,
    POLICY_DENIED_CONTINUE_MSG,
    POLICY_EVALUATOR_ERROR_CODE,
    POLICY_EVALUATOR_ERROR_MSG,
    POLICY_GATEWAY_STAMP_KEY,
)
from adgn.mcp.approval_policy.clients import PolicyReaderStub

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _raise_if_reserved_code(e: McpError, name: str) -> None:
    # Some backends may rewrap or drop the ErrorData; handle both code and message checks.
    code: int | None = None
    msg: str | None = None
    stamped: bool = False
    error = e.error
    try:
        code = int(error.code)
    except Exception:
        code = None
    try:
        msg = str(error.message)
    except Exception:
        msg = None
    data = error.data
    if isinstance(data, dict) and data.get(POLICY_GATEWAY_STAMP_KEY) is True:
        stamped = True
    if msg is None:
        msg = str(e)
    # Inspect args for embedded ErrorData/dict with stamp (in-proc raises may drop .error)
    if not stamped:
        for a in e.args:
            if isinstance(a, mtypes.ErrorData):
                data = a.data
                if isinstance(data, dict) and data.get(POLICY_GATEWAY_STAMP_KEY) is True:
                    stamped = True
                    break
            if isinstance(a, dict):
                try:
                    ad = mtypes.ErrorData.model_validate(a)
                    data = ad.data
                    if isinstance(data, dict) and data.get(POLICY_GATEWAY_STAMP_KEY) is True:
                        stamped = True
                        break
                except Exception:
                    data = a.get("data")
                    if isinstance(data, dict) and data.get(POLICY_GATEWAY_STAMP_KEY) is True:
                        stamped = True
                        break

    if (
        stamped
        or (code in (POLICY_DENIED_ABORT_CODE, POLICY_DENIED_CONTINUE_CODE, POLICY_EVALUATOR_ERROR_CODE))
        or (msg in (POLICY_DENIED_ABORT_MSG, POLICY_DENIED_CONTINUE_MSG, POLICY_EVALUATOR_ERROR_MSG))
    ):
        raise McpError(
            ErrorData(
                code=POLICY_BACKEND_RESERVED_MISUSE_CODE,
                message=POLICY_BACKEND_RESERVED_MISUSE_MSG,
                data={
                    POLICY_GATEWAY_STAMP_KEY: True,
                    "name": name,
                    "backend_code": code if code is not None else "unknown",
                },
            )
        )


_DENIAL_MAP: dict[ApprovalDecision, tuple[int, str]] = {
    ApprovalDecision.DENY_ABORT: (POLICY_DENIED_ABORT_CODE, POLICY_DENIED_ABORT_MSG),
    ApprovalDecision.DENY_CONTINUE: (POLICY_DENIED_CONTINUE_CODE, POLICY_DENIED_CONTINUE_MSG),
}


def _policy_denied_error(decision: ApprovalDecision, name: str, reason: str | None) -> McpError:
    code, msg = _DENIAL_MAP[decision]
    return McpError(
        ErrorData(
            code=code,
            message=msg,
            data={POLICY_GATEWAY_STAMP_KEY: True, "decision": str(decision), "name": name, "reason": reason},
        )
    )


class PolicyGatewayMiddleware(Middleware):
    """Approval-enforcing middleware installed on the aggregating FastMCP server.

    - Gates tools/call via the provided policy evaluator and ApprovalHub
    - Denials raise explicit JSON-RPC errors using reserved codes/messages
    - ASK blocks until ApprovalHub resolves to Continue/Abort/Bypass
    """

    def __init__(
        self,
        *,
        hub: ApprovalHub,
        policy_reader: PolicyReaderStub,
        persistence: Persistence,
        agent_id: AgentID,
        pending_notifier: Callable[[ToolCall], Awaitable[None]] | None = None,
        run_id: UUID | None = None,
    ) -> None:
        self._hub = hub
        self._notify = pending_notifier
        self._policy_reader = policy_reader
        self._persistence = persistence
        self._run_id = run_id
        self._agent_id = agent_id

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, ToolResult]) -> ToolResult:
        name = context.message.name
        arguments = context.message.arguments
        tool_key = name  # canonical function name
        call_id = "pg:" + uuid.uuid4().hex

        # Create PENDING tool call record
        tool_call = ToolCall(name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None)
        pending_record = ToolCallRecord(
            call_id=call_id,
            run_id=str(self._run_id) if self._run_id is not None else None,
            agent_id=self._agent_id,
            tool_call=tool_call,
            decision=None,
            execution=None,
        )
        await self._persistence.save_tool_call(pending_record)

        # Evaluate decision via MCP reader server when available; fallback to local evaluator
        try:
            decision_res = await self._policy_reader.decide(PolicyRequest(name=name, arguments=arguments))
            decision = decision_res.decision
            rationale = decision_res.rationale
        except Exception as e:  # policy engine failure → explicit evaluator error
            logger.warning("policy evaluator error", exc_info=e)
            raise McpError(
                ErrorData(
                    code=POLICY_EVALUATOR_ERROR_CODE,
                    message=POLICY_EVALUATOR_ERROR_MSG,
                    data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "reason": f"{type(e).__name__}: {e}"},
                )
            )

        logger.debug("Policy decision: %s → %s (%s)", name, decision, rationale or "")

        if decision is ApprovalDecision.ALLOW:
            # Update with decision (PENDING → EXECUTING)
            decision_obj = Decision(outcome=ApprovalStatus.APPROVED, decided_at=_now(), reason=rationale)
            executing_record = ToolCallRecord(
                call_id=call_id,
                run_id=str(self._run_id) if self._run_id is not None else None,
                agent_id=self._agent_id,
                tool_call=ToolCall(name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None),
                decision=decision_obj,
                execution=None,
            )
            await self._persistence.save_tool_call(executing_record)

            try:
                call_result = await call_next(context)

                # Update with execution result (EXECUTING → COMPLETED)
                execution_obj = ToolCallExecution(completed_at=_now(), output=convert_fastmcp_result(call_result))
                completed_record = ToolCallRecord(
                    call_id=call_id,
                    run_id=str(self._run_id) if self._run_id is not None else None,
                    agent_id=self._agent_id,
                    tool_call=ToolCall(
                        name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None
                    ),
                    decision=decision_obj,
                    execution=execution_obj,
                )
                await self._persistence.save_tool_call(completed_record)

                # If downstream returned an error ToolResult instead of raising,
                # remap reserved policy codes/messages here using typed parsing when available.
                if bool(call_result.is_error):
                    # Parse error details - ErrorData guarantees code: int per MCP/JSON-RPC spec
                    err = call_result.error
                    if err is None:
                        return call_result

                    # Try parsing as ErrorData (validates code is int, message is str)
                    try:
                        ed = mtypes.ErrorData.model_validate(err)
                    except Exception:
                        # Non-conforming error format - pass through
                        return call_result

                    # Check if error uses reserved policy codes/messages
                    stamped_downstream = isinstance(ed.data, dict) and ed.data.get(POLICY_GATEWAY_STAMP_KEY) is True
                    if (
                        stamped_downstream
                        or ed.code
                        in (POLICY_DENIED_ABORT_CODE, POLICY_DENIED_CONTINUE_CODE, POLICY_EVALUATOR_ERROR_CODE)
                        or ed.message
                        in (POLICY_DENIED_ABORT_MSG, POLICY_DENIED_CONTINUE_MSG, POLICY_EVALUATOR_ERROR_MSG)
                    ):
                        raise McpError(
                            ErrorData(
                                code=POLICY_BACKEND_RESERVED_MISUSE_CODE,
                                message=POLICY_BACKEND_RESERVED_MISUSE_MSG,
                                data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "backend_code": ed.code},
                            )
                        )
                return call_result
            except McpError as e:
                _raise_if_reserved_code(e, name)
                raise
            except Exception as e:
                # Some servers may translate backend McpError into a ToolError before it reaches us.
                # As a last resort, remap by inspecting the exception text.
                s = str(e)
                if (
                    (POLICY_DENIED_ABORT_MSG in s)
                    or (POLICY_DENIED_CONTINUE_MSG in s)
                    or (POLICY_EVALUATOR_ERROR_MSG in s)
                ):
                    raise McpError(
                        ErrorData(
                            code=POLICY_BACKEND_RESERVED_MISUSE_CODE,
                            message=POLICY_BACKEND_RESERVED_MISUSE_MSG,
                            data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "backend_code": "unknown"},
                        )
                    )
                raise

        if decision is ApprovalDecision.DENY_ABORT:
            # Update with decision (no execution)
            decision_obj = Decision(outcome=ApprovalStatus.ABORTED, decided_at=_now(), reason=rationale)
            denied_record = ToolCallRecord(
                call_id=call_id,
                run_id=str(self._run_id) if self._run_id is not None else None,
                agent_id=self._agent_id,
                tool_call=ToolCall(name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None),
                decision=decision_obj,
                execution=None,
            )
            await self._persistence.save_tool_call(denied_record)

            raise _policy_denied_error(ApprovalDecision.DENY_ABORT, name, rationale)

        if decision is ApprovalDecision.DENY_CONTINUE:
            # Update with decision (no execution)
            decision_obj = Decision(outcome=ApprovalStatus.DENIED, decided_at=_now(), reason=rationale)
            denied_record = ToolCallRecord(
                call_id=call_id,
                run_id=str(self._run_id) if self._run_id is not None else None,
                agent_id=self._agent_id,
                tool_call=ToolCall(name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None),
                decision=decision_obj,
                execution=None,
            )
            await self._persistence.save_tool_call(denied_record)

            raise _policy_denied_error(ApprovalDecision.DENY_CONTINUE, name, rationale)

        # ASK: block until resolved via ApprovalHub
        tool_call = ToolCall(name=name, call_id=call_id, args_json=(json.dumps(arguments) if arguments else None))
        # Register + notify before awaiting
        wait_coro = self._hub.await_decision(call_id, tool_call)
        if self._notify is not None:
            await self._notify(tool_call)

        decision_response = await wait_coro

        if isinstance(decision_response, ContinueDecision):
            # User approved - update with decision (PENDING → EXECUTING)
            decision_obj = Decision(outcome=ApprovalStatus.APPROVED, decided_at=_now(), reason=None)
            executing_record = ToolCallRecord(
                call_id=call_id,
                run_id=str(self._run_id) if self._run_id is not None else None,
                agent_id=self._agent_id,
                tool_call=ToolCall(name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None),
                decision=decision_obj,
                execution=None,
            )
            await self._persistence.save_tool_call(executing_record)

            try:
                call_result = await call_next(context)

                # Update with execution result (EXECUTING → COMPLETED)
                execution_obj = ToolCallExecution(completed_at=_now(), output=convert_fastmcp_result(call_result))
                completed_record = ToolCallRecord(
                    call_id=call_id,
                    run_id=str(self._run_id) if self._run_id is not None else None,
                    agent_id=self._agent_id,
                    tool_call=ToolCall(
                        name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None
                    ),
                    decision=decision_obj,
                    execution=execution_obj,
                )
                await self._persistence.save_tool_call(completed_record)

                return call_result
            except McpError as e:
                _raise_if_reserved_code(e, name)
                raise

        if isinstance(decision_response, AbortTurnDecision):
            # User denied - update with decision (no execution)
            decision_obj = Decision(
                outcome=ApprovalStatus.ABORTED, decided_at=_now(), reason=decision_response.reason
            )
            denied_record = ToolCallRecord(
                call_id=call_id,
                run_id=str(self._run_id) if self._run_id is not None else None,
                agent_id=self._agent_id,
                tool_call=ToolCall(name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None),
                decision=decision_obj,
                execution=None,
            )
            await self._persistence.save_tool_call(denied_record)

            raise _policy_denied_error(ApprovalDecision.DENY_ABORT, name, decision_response.reason)

        # Unknown decision type: internal error for visibility
        raise McpError(
            ErrorData(
                code=-32603,
                message="internal_error: unknown approval decision type",
                data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "decision_type": type(decision_response).__name__},
            )
        )


def install_policy_gateway(
    comp: Any,
    *,
    hub: ApprovalHub,
    policy_reader: PolicyReaderStub,
    persistence: Persistence,
    agent_id: AgentID,
    pending_notifier: Callable[[ToolCall], Awaitable[None]] | None = None,
    run_id: UUID | None = None,
) -> None:
    """Install PolicyGatewayMiddleware on a FastMCP-like server.

    This mirrors production wiring in the container; tests should reuse this
    helper to avoid drift in middleware configuration.
    """
    comp.add_middleware(
        PolicyGatewayMiddleware(
            hub=hub,
            policy_reader=policy_reader,
            persistence=persistence,
            agent_id=agent_id,
            pending_notifier=pending_notifier,
            run_id=run_id,
        )
    )
