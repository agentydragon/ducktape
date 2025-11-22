"""MiniCodex agent on OpenAI Responses API with MCP tool wiring.

For stateless reasoning/tool replay demo, see :/adgn/examples/openai_api/stateless_two_step_demo.py
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
import copy
from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING, Any

import anyio
from fastmcp.client import Client
from fastmcp.client.client import CallToolResult
from mcp import types as mcp_types
from pydantic import TypeAdapter

from adgn.agent.handler import AssistantText, GroundTruthUsage, Response, ToolCall, ToolCallOutput, UserText
from adgn.agent.loop_control import Abort, Auto, Continue, Forbid, RequireAny, RequireSpecific, ToolPolicy
from adgn.mcp._shared.calltool import serialize_tool_result_compact
from adgn.openai_utils.model import (
    AssistantMessage,
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    FunctionToolParam,
    InputItem,
    OpenAIModelProto,
    ReasoningItem,
    ResponsesRequest,
    SystemMessage,
    ToolChoice,
    ToolChoiceFunction,
    UserMessage,
)
from adgn.openai_utils.types import ReasoningEffort, ReasoningSummary, build_reasoning_params

from .reducer import BaseHandler, Reducer


@dataclass
class AgentResult:
    text: str
    # NOTE: We intentionally do NOT return transcript/events in agent result.
    # Tests or callers that need access to the event sequence should register a handler
    # (e.g. a test-only RecordingHandler) and pass it via `handlers` argument to MiniCodex.create().


@dataclass(slots=True)
class ToolCallSuccess:
    """Successful MCP tool invocation."""

    result: CallToolResult


@dataclass(slots=True)
class ToolCallFailure:
    """MCP invocation failed; carries the structured tool result."""

    result: CallToolResult
    reason: str | None = None


@dataclass(slots=True)
class ToolCallAborted:
    """Invocation aborted (policy/UI); embeds synthetic structured error."""

    result: CallToolResult
    reason: str | None = None


ToolCallOutcome = ToolCallSuccess | ToolCallFailure | ToolCallAborted


# Copying helper was trivial; inline deepcopy at call sites to avoid indirection


def _require_call_id(function_call: FunctionCallItem) -> str:
    call_id = function_call.call_id
    if not isinstance(call_id, str) or not call_id:
        raise RuntimeError("FunctionCallItem missing call_id")
    return call_id


def _dump_call_tool_result(res: CallToolResult, tool_call_info: str | None = None) -> str:
    """Serialize an MCP CallToolResult for Responses input (native field names).

    Dumps a compact JSON with native snake_case keys to avoid lossy remapping.
    """

    result = json.dumps(serialize_tool_result_compact(res), ensure_ascii=False)

    # Safety check: OpenAI has a 10MB limit for input strings
    # Fail fast if tool output is too large to prevent API errors
    if len(result) > MAX_TOOL_RESULT_BYTES:
        error_msg = (
            f"Tool output too large: {len(result) / (1024 * 1024):.1f}MB "
            f"exceeds max {MAX_TOOL_RESULT_BYTES / (1024 * 1024):.0f}MB. "
        )
        if tool_call_info:
            error_msg += f" Tool call: {tool_call_info}."
        error_msg += " MCP server returned oversized result - check slicing/pagination."
        raise RuntimeError(error_msg)

    return result


def _maybe_error_message(res: CallToolResult) -> str | None:
    if not res.is_error:
        return None
    structured = res.structured_content
    if isinstance(structured, dict):
        err = structured.get("error")
        if isinstance(err, str) and err:
            return err
    for block in res.content or []:
        # Only support plain text blocks for now; surface others explicitly.
        if isinstance(block, mcp_types.TextContent):
            # Do not strip content; return exactly as provided by the server.
            txt = block.text
            if isinstance(txt, str) and txt:
                return txt
        else:
            # TODO(mpokorny): Support non-text content in agent error handling:
            #  - mcp_types.ImageContent: decode or summarize
            #  - mcp_types.AudioContent: transcribe or summarize
            #  - mcp_types.ResourceLink / EmbeddedResource: fetch and summarize safely
            raise NotImplementedError(f"Unsupported CallToolResult content type: {type(block).__name__}")
    return None


def _make_error_result(message: str) -> CallToolResult:
    return CallToolResult(content=[], structured_content={"ok": False, "error": message}, is_error=True)


DEFAULT_ABORT_ERROR = "tool execution aborted"


def _abort_result(reason: str | None = None) -> CallToolResult:
    return _make_error_result(reason or DEFAULT_ABORT_ERROR)




def _call_tool_result_from_json(output: str) -> CallToolResult:
    """Parse CallToolResult from JSON using Pydantic for validation.

    Expects native snake_case keys; raises ValueError on invalid payload.
    """
    return TypeAdapter(CallToolResult).validate_json(output)


# Namespaced tool form: mcp_{server}_{tool}
ToolMap = dict[str, Any]

SYSTEM_INSTRUCTIONS = "You are a code agent. Be concise."

# Size limits (bytes)
MAX_TOOL_RESULT_BYTES = 10 * 1024 * 1024  # 10 MiB


def _tool_choice_from_policy(policy: ToolPolicy) -> ToolChoice:
    """Map a ToolPolicy to Responses API tool_choice value.

    Exhaustive and strict: raises on unknown policy; RequireSpecific supports exactly one name.
    """
    if isinstance(policy, RequireAny):
        return "required"
    if isinstance(policy, Auto):
        return "auto"
    if isinstance(policy, Forbid):
        return "none"
    if isinstance(policy, RequireSpecific):
        if len(policy.names) == 1:
            return ToolChoiceFunction(name=policy.names[0])
        raise ValueError("RequireSpecific with multiple names is not supported for Responses.tool_choice")
    raise TypeError(f"Unknown ToolPolicy: {type(policy).__name__}")


type Message = UserMessage | AssistantMessage | SystemMessage
type TranscriptItem = Message | FunctionCallItem | ReasoningItem | ToolCallOutput


class MiniCodex:
    def __init__(
        self,
        *,
        model: str,
        system: str | None,
        mcp_client: Client,
        client: OpenAIModelProto,
        reasoning_effort: ReasoningEffort | None = None,
        reasoning_summary: ReasoningSummary | None = None,
        parallel_tool_calls: bool,
        handlers: Iterable[BaseHandler],
        dynamic_instructions: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._model = model
        self._default_system = system or SYSTEM_INSTRUCTIONS
        self._system = self._default_system
        self._dynamic_instructions = dynamic_instructions
        self._mcp_client = mcp_client
        self._client = client
        self._parallel_tool_calls = parallel_tool_calls
        self._transcript: list[TranscriptItem] = []
        self._reasoning_effort = reasoning_effort
        self._reasoning_summary = reasoning_summary
        # Agent state fields
        self.assistant_text_chunks: list[str] = []
        self.pending_function_calls: list[FunctionCallItem] = []
        self.finished: bool = False
        # Track function calls for debugging
        self._function_call_map: dict[str, FunctionCallItem] = {}
        # Aggregating controller (owns handlers and loop-decision semantics)
        handlers_list = list(handlers)
        assert handlers_list, "At least one handler required; add AutoHandler() or a control handler"
        self._controller = Reducer(handlers_list)

    def set_system_instructions(self, instructions: str | None) -> None:
        """Override base system instructions for future turns."""
        self._system = (instructions or self._default_system).strip()

    async def _build_effective_instructions(self) -> str:
        # Compose dynamic instructions if a provider is set; append to base system.
        if self._dynamic_instructions is not None:
            dyn = await self._dynamic_instructions()
            base = self._system or ""
            return (base + (dyn or "")).strip()
        return (self._system or "").strip()

    async def run(self, user_text: str) -> AgentResult:
        self._transcript.append(UserMessage.text(user_text))
        self._controller.on_user_text(UserText(text=user_text))
        self.assistant_text_chunks.clear()
        self.pending_function_calls.clear()
        self.finished = False
        try:
            while not self.finished:
                # Pre-phase inserts now handled by handlers via Continue.inserts_input
                await self._run_one_phase()
                if self.pending_function_calls:
                    await self._handle_pending_tool_calls()
            return AgentResult(text="\n".join(self.assistant_text_chunks))
        except Exception as exc:
            self._controller.on_error(exc)
            raise

    async def _handle_pending_tool_calls(self) -> None:
        function_calls: list[FunctionCallItem] = list(self.pending_function_calls)

        local_result_map: dict[str, CallToolResult] = {
            evt.call_id: evt.result for evt in self._transcript if isinstance(evt, ToolCallOutput)
        }

        async def _invoke(
            function_call: FunctionCallItem,
            local_map: dict[str, CallToolResult] = local_result_map,
        ) -> ToolCallOutcome:
            cid = _require_call_id(function_call)
            # No agent-level before-tool gating; Policy Gateway middleware enforces approvals/denials
            if cid in local_map:
                if (cached := copy.deepcopy(local_map[cid])).is_error:
                    return ToolCallFailure(result=cached, reason=_maybe_error_message(cached))
                return ToolCallSuccess(result=cached)

            # Invoke via Policy Gateway client; do not swallow exceptions.
            # Parse arguments strictly; invalid JSON/object shape is a hard error.
            args_json = function_call.arguments
            args: dict[str, Any] = {}
            if args_json:
                val = json.loads(args_json)
                if not isinstance(val, dict):
                    raise ValueError("tool arguments must be a JSON object")
                args = val
            raw = await self._mcp_client.call_tool(function_call.name, args, raise_on_error=False)
            res = copy.deepcopy(raw)
            if res.is_error:
                return ToolCallFailure(result=res, reason=_maybe_error_message(res))
            return ToolCallSuccess(result=res)

        if self._parallel_tool_calls:
            await self._run_tool_calls_parallel(function_calls, _invoke)
        else:
            await self._run_tool_calls_sequential(function_calls, _invoke)
        self.pending_function_calls.clear()

    async def _run_tool_calls_parallel(
        self, function_calls: list[FunctionCallItem], invoker
    ) -> None:
        results: dict[str, ToolCallOutcome] = {}
        abort_triggered = False

        async with anyio.create_task_group() as tg:
            cancelled_exc = anyio.get_cancelled_exc_class()

            async def runner(fc: FunctionCallItem) -> None:
                nonlocal abort_triggered
                try:
                    outcome = await invoker(fc)
                except cancelled_exc:
                    return
                cid = _require_call_id(fc)
                results[cid] = outcome
                if isinstance(outcome, ToolCallAborted):
                    abort_triggered = True
                    tg.cancel_scope.cancel()

            for function_call in function_calls:
                tg.start_soon(runner, function_call)

        had_error = False
        for function_call in function_calls:
            cid = _require_call_id(function_call)
            if (outcome := results.get(cid)) is None:
                if not abort_triggered:
                    raise RuntimeError(f"Missing tool output for call_id={cid!r}")
                outcome = ToolCallAborted(result=_abort_result())
            self._emit_tool_result(function_call, outcome.result)
            if isinstance(outcome, ToolCallAborted):
                had_error = True
        if had_error:
            self.finished = True

    async def _run_tool_calls_sequential(
        self, function_calls: list[FunctionCallItem], invoker
    ) -> None:
        for i, function_call in enumerate(function_calls):
            outcome = await invoker(function_call)
            self._emit_tool_result(function_call, outcome.result)
            if isinstance(outcome, ToolCallAborted):
                for remaining in function_calls[i + 1 :]:
                    self._emit_tool_result(remaining, _abort_result())
                self.finished = True
                break

    def _to_openai_input_items(self) -> list[InputItem]:
        """Convert transcript to typed OpenAI Responses input items."""
        items: list[InputItem] = []
        for item in self._transcript:
            if isinstance(item, UserMessage | AssistantMessage | SystemMessage):
                items.append(item.model_copy(deep=True))
                continue
            if isinstance(item, ReasoningItem):
                items.append(item)
                continue
            if isinstance(item, FunctionCallItem):
                items.append(item)
                continue
            if isinstance(item, ToolCallOutput):
                # Look up the function call from our map for debugging info
                tool_info = f"call_id={item.call_id}"
                if item.call_id in self._function_call_map:
                    fc = self._function_call_map[item.call_id]
                    tool_info = f"{fc.name}(call_id={item.call_id})"

                items.append(
                    FunctionCallOutputItem(call_id=item.call_id, output=_dump_call_tool_result(item.result, tool_info))
                )
                continue
            raise TypeError(f"Unsupported transcript item for OpenAI input: {type(item)}")
        return items

    async def _run_one_phase(self):
        decision = self._controller.on_before_sample()
        if isinstance(decision, Abort):
            self.finished = True
            return
        # Unify resp_output element type across branches for mypy
        resp_output: list[ReasoningItem | FunctionCallItem | FunctionCallOutputItem | AssistantMessageOut] | None = None
        if isinstance(decision, Continue) and decision.skip_sampling:
            # Skip sampling: treat handler-provided inserts_input as if they were
            # model output items for this phase and process them via the normal
            # output path (adds assistant text, enqueues tool calls, etc.).
            # Caller must ensure inserts_input contains only output-side items when skip_sampling=True
            resp_output = list(decision.inserts_input)  # Trust type system
        elif isinstance(decision, Continue):
            # Inject any handler-provided pre-sample inserts into transcript
            # Runtime check: FunctionCallItem only allowed with skip_sampling=True
            if any(isinstance(item, FunctionCallItem) for item in decision.inserts_input):
                raise TypeError("FunctionCallItem requires skip_sampling=True")
            self._transcript.extend(decision.inserts_input)
            tool_choice = _tool_choice_from_policy(decision.tool_policy)
            reasoning_param = build_reasoning_params(self._reasoning_effort, self._reasoning_summary)
            # Build OpenAI Responses tools list via Policy Gateway client (proxy aggregates downstream)
            tools = await self._mcp_client.list_tools()

            req = ResponsesRequest(
                input=self._to_openai_input_items(),
                instructions=await self._build_effective_instructions(),
                stream=False,
                tool_choice=tool_choice,
                store=True,
                parallel_tool_calls=self._parallel_tool_calls,
                tools=[
                    FunctionToolParam(name=t.name, description=t.description, parameters=t.inputSchema) for t in tools
                ],
                reasoning=reasoning_param,
            )
            resp = await self._client.responses_create(req)
            sdk_usage = resp.usage
            usage = (
                GroundTruthUsage(
                    model=self._model,
                    input_tokens=sdk_usage.input_tokens,
                    input_tokens_details=sdk_usage.input_tokens_details,
                    output_tokens=sdk_usage.output_tokens,
                    output_tokens_details=sdk_usage.output_tokens_details,
                    total_tokens=sdk_usage.total_tokens,
                )
                if sdk_usage is not None
                else GroundTruthUsage(model=self._model)
            )
            self._controller.on_response(Response(response_id=resp.id, usage=usage, model=self._model))
            resp_output = resp.output
        else:
            raise TypeError(f"Unsupported loop decision: {type(decision).__name__}")
        if resp_output is not None:
            self._process_resp_output(resp_output)
        if not self.pending_function_calls:
            self.finished = True

    def _process_resp_output(
        self, resp_output: Sequence[ReasoningItem | FunctionCallItem | FunctionCallOutputItem | AssistantMessageOut]
    ) -> None:
        self.pending_function_calls.clear()
        # Skip items that are already present in our transcript (id collision).
        existing_ids: set[str] = set()
        for evt in self._transcript:
            # Only these item types carry optional id fields
            if isinstance(evt, ReasoningItem | FunctionCallItem):
                eid = evt.id
                if isinstance(eid, str) and eid:
                    existing_ids.add(eid)
        handled_cids = {evt.call_id for evt in self._transcript if isinstance(evt, ToolCallOutput)}
        for item in resp_output:
            # If this item has an id and we've already recorded it, skip
            iid = item.id if isinstance(item, ReasoningItem | FunctionCallItem) else None
            if isinstance(iid, str) and iid in existing_ids:
                continue
            if isinstance(item, ReasoningItem):
                self._controller.on_reasoning(item)
                self._transcript.append(item)
            elif isinstance(item, AssistantMessageOut):
                text = item.text
                self.assistant_text_chunks.append(text)
                self._controller.on_assistant_text(AssistantText(text=text))
                # Store assistant as our input item type to avoid secondary translation
                self._transcript.append(item.to_input_item())
            elif isinstance(item, FunctionCallOutputItem):
                try:
                    if item.output is None:
                        raise ValueError("FunctionCallOutputItem.output is None")
                    result = _call_tool_result_from_json(item.output)
                except ValueError as exc:  # pragma: no cover - defensive
                    raise ValueError(f"Failed to parse CallToolResult for call_id={item.call_id}") from exc
                ocid = item.call_id
                if not isinstance(ocid, str) or not ocid:
                    raise RuntimeError("FunctionCallOutputItem missing call_id")
                event = ToolCallOutput(call_id=ocid, result=result)
                handled_cids.add(ocid)
                self._controller.on_tool_result(event)
                self._transcript.append(event)
                if self.pending_function_calls:
                    self.pending_function_calls = [fc for fc in self.pending_function_calls if fc.call_id != ocid]
            elif isinstance(item, FunctionCallItem):
                # Enforce a proper call_id for indexing/pending management
                cid = _require_call_id(item)
                fc_local = item  # No conversion needed anymore
                self._controller.on_tool_call(ToolCall(name=item.name, args_json=item.arguments, call_id=cid))
                self._transcript.append(fc_local)
                # Store in map for quick lookup when processing outputs
                self._function_call_map[cid] = fc_local
                if cid in handled_cids:
                    continue
                self.pending_function_calls.append(fc_local)
            else:
                # Crash fast on unknown items to surface mismatches early
                raise TypeError(f"Unsupported Responses output item: {type(item)}")

    @classmethod
    async def create(
        cls,
        *,
        model: str,
        mcp_client: Client,
        handlers: Iterable[BaseHandler],
        client: OpenAIModelProto,
        system: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        reasoning_summary: ReasoningSummary | None = None,
        parallel_tool_calls: bool = True,
        dynamic_instructions: Callable[[], Awaitable[str]] | None = None,
    ) -> MiniCodex:
        return cls(
            model=model,
            system=system,
            mcp_client=mcp_client,
            client=client,
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
            parallel_tool_calls=parallel_tool_calls,
            handlers=list(handlers),
            dynamic_instructions=dynamic_instructions,
        )

    def _emit_tool_result(self, function_call: FunctionCallItem, result: CallToolResult) -> None:
        """Emit a ToolCallOutput event and notify handlers."""

        call_id = _require_call_id(function_call)
        event = ToolCallOutput(call_id=call_id, result=copy.deepcopy(result))
        self._transcript.append(event)
        self._controller.on_tool_result(event)

    # Exposed for abort flows: synthesize aborted outputs for all pending calls
    def abort_pending_tool_calls(self) -> None:
        if not self.pending_function_calls:
            return
        for fc in list(self.pending_function_calls):
            self._emit_tool_result(fc, _abort_result())
        self.pending_function_calls.clear()

    @property
    def messages(self) -> list[InputItem]:
        """Format transcript for OpenAI Responses API.

        Summary of our reasoning handling (stateless, full-input):
        - We forward the exact ResponseReasoningItem objects returned by the model
          in-order as part of the transcript when continuing the model's chain-of-thought.
        - We do NOT synthesize or mutate reasoning items or ids; always forward the
          SDK-returned objects (model_dump(exclude_none=True)).
        - We avoid previous_response_id / stateful Responses API usage by design and
          therefore reproduce the full input sequence (user/assistant/reasoning/
          function_call/function_call_output) on each stateless request.
        - Reasoning forwarding is orthogonal to tool execution: include reasoning
          items where they were produced to allow the model to continue reasoning.

        Recommended/required practices:
        - Preserve ordering and structure exactly as returned by the SDK/API.
        - Do not fabricate rs_/fc_ ids; prefer omission over synthesis if originals
          are missing.

        Canonical references:
        - OpenAI Responses API reference: https://platform.openai.com/docs/api-reference/responses
        - OpenAI Cookbook examples (reasoning items & function-call orchestration):
          https://github.com/openai/openai-cookbook/blob/main/examples/responses_api/reasoning_items.ipynb
          https://github.com/openai/openai-cookbook/blob/main/examples/reasoning_function_calls.ipynb

        Implementation note: this agent intentionally uses the stateless full-input
        approach to preserve reproducibility and avoid server-side state. Keep this
        behavior in mind when modifying messages()/transcript serialization.
        """
        return self._to_openai_input_items()

    async def __aenter__(self) -> MiniCodex:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


logger = logging.getLogger(__name__)
