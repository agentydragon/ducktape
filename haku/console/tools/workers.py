"""haku-console's in-process ``workers`` MCP server — dispatching one-shot hosted workers.

A strong orchestrator session decomposes work into well-specified subtasks, fans them onto abundant
lower-tier workers, and retrieves the results (#5193). ``dispatch_worker`` is the spawn half of that
loop: it opens a conversation for a named worker Agent on a named harness — reusing the exact
agent+harness selection and session/sandbox allocation the operator-initiated ``createConversation``
flow uses — seeds the opening operator-origin prompt so the worker starts on its first turn, and
returns the new session id immediately without awaiting the work (the long-await is a later PR).

**The worker runs under its own perimeter.** The dispatched session's launch identity is the named
worker Agent, so the session carries that Agent's own grants and fence identity; dispatching never
runs the worker under the caller's identity or widens its reach (#5193). Selection is the launch
authorizer's: an Agent that is not launchable, or a harness its access profile disallows, is
refused here exactly as it is on the console's own launch path.

**Approval-gated per dispatch, like ``create_grant``.** ``dispatch_worker`` is never auto-approved,
so an Operator rules on each spawn in the drawer before the conversation is created; the outer
Console MCP boundary returns the pending-approval stub and resolves it on approval. The tool also
refuses to run for an Agent caller with no approving Operator, so the gate holds even against a
misconfigured auto-approval policy.
"""

from __future__ import annotations

from typing import Annotated, assert_never
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from haku.console.conversation.prompt_origin import SPA_ORIGIN
from haku.console.harnesses.kind import HarnessKind
from haku.console.mcp.execution import (
    EXECUTION_CONTEXT_DEPENDENCY,
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
)
from haku.console.session.launch_identity import LaunchAgentRejectedError
from haku.console.session.runtime import SessionService

# Wire-frozen id: named by cluster/k8s/haku/console/config.yaml (mcp.servers + a profile's
# in_process_server_ids). Renaming is a config change.
WORKERS_SERVER_ID = "workers"


class DispatchedWorker(BaseModel):
    """The hosted worker session ``dispatch_worker`` opened and seeded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID = Field(
        description="The new worker session. Poll its outcome with the conversation read tools (get_worker_result)."
    )


def _dispatching_operator(context: McpExecutionContext) -> UUID:
    """The Operator the dispatched worker session is opened for.

    An Agent caller reaches execution only after an Operator approves the dispatch in the drawer, so
    the approving Operator owns the worker session — and must own the worker Agent, or the launch
    authorizer refuses it. The worker's own launch identity is what fences it; the owning Operator
    is only who the session belongs to. An Operator calling directly opens it under their own
    identity.
    """
    match context.caller:
        case OperatorMcpExecutionCaller(operator_id=operator_id):
            return operator_id
        case AgentMcpExecutionCaller():
            if context.approving_operator_id is None:
                raise ToolError("dispatch_worker requires per-call Operator approval before it can run")
            return context.approving_operator_id
    assert_never(context.caller)


def build_mcp(sessions: SessionService) -> FastMCP:
    """Build the one-tool ``workers`` server over the console's session runtime."""

    mcp: FastMCP = FastMCP(
        name=WORKERS_SERVER_ID,
        instructions=(
            "Dispatch a one-shot hosted worker: open a conversation for a worker Agent on a harness, "
            "seed its opening prompt, and get back the session id immediately without awaiting the work. "
            "The worker runs under its own Agent perimeter (its own grants and fence identity), not "
            "yours. Every dispatch is reviewed and approved by the Operator per call."
        ),
    )

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=False, openWorldHint=False))
    async def dispatch_worker(
        agent_id: Annotated[
            UUID,
            Field(
                description="The worker Agent to run the session as. The session carries this Agent's own grants and "
                "fence identity, not the caller's."
            ),
        ],
        harness_kind: Annotated[
            HarnessKind,
            Field(description="Which harness the worker runs (e.g. 'codex_app_server' for the public-coder worker)."),
        ],
        prompt: Annotated[
            str,
            Field(
                min_length=1,
                max_length=100_000,
                description="The opening prompt, seeded as the worker's first operator-origin turn.",
            ),
        ],
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> DispatchedWorker:
        """Open a hosted worker session for (agent_id, harness_kind), seed the prompt, and return its session id.

        Returns immediately without awaiting the worker's work. Reuses the operator-initiated
        conversation-creation path, so the worker's session and sandbox are allocated exactly as a
        console-launched session's are, and the worker starts on the seeded prompt as its first turn.
        """
        operator_id = _dispatching_operator(context)
        try:
            conversation = await sessions.create_conversation(operator_id, agent_id=agent_id, harness_kind=harness_kind)
        except LaunchAgentRejectedError:
            # The durable reason is deliberately not surfaced (same generic refusal the console's own
            # launch route gives): a launchable-Agent probe must not read back why one was rejected.
            raise ToolError("the selected worker Agent is not launchable on this harness") from None
        await sessions.enqueue_conversation_prompt(operator_id, conversation.conversation_id, prompt, SPA_ORIGIN)
        return DispatchedWorker(session_id=conversation.session.session_id)

    return mcp
