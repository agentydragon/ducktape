from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pydantic_core
from pydantic import TypeAdapter

from agent_core.events import (
    ApiRequest,
    AssistantText,
    GroundTruthUsage,
    Response,
    SystemText,
    ToolCall,
    ToolCallOutput,
    UserText,
)
from agent_core.handler import BaseHandler
from agent_core.loop_control import (
    Abort,
    AllowAnyToolOrTextMessage,
    Compact,
    InjectItems,
    LoopDecision,
    NoAction,
    RequireAnyTool,
    ToolPolicy,
)
from agent_core.tool_provider import ImageContent, ResultContent, TextContent, ToolOutputData, ToolProvider, ToolResult
from openai_utils.model import (
    AssistantMessage,
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    FunctionToolParam,
    InputItem,
    Model,
    ReasoningItem,
    ResponsesRequest,
    SystemMessage,
    ToolChoice,
    UserMessage,
)
from openai_utils.pydantic_strict_mode import validate_openai_strict_mode_schema
from openai_utils.types import ReasoningEffort, ReasoningSummary, build_reasoning_params

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    """`done` reflects handler completion; output remains with handlers."""

    done: bool


@dataclass
class CompactionResult:
    compacted: bool


def _make_strict_function_tool(tool_name: str, description: str, input_schema: dict[str, Any]) -> FunctionToolParam:
    validate_openai_strict_mode_schema(input_schema, tool_name)
    return FunctionToolParam(name=tool_name, description=description, parameters=input_schema, strict=True)


def _require_call_id(function_call: FunctionCallItem) -> str:
    call_id = function_call.call_id
    if not call_id:
        raise RuntimeError("FunctionCallItem missing call_id")
    return call_id


MAX_TOOL_RESULT_BYTES = 10 * 1024 * 1024  # 10 MiB


def _check_size(result: str) -> None:
    if len(result) > MAX_TOOL_RESULT_BYTES:
        raise RuntimeError(f"Tool output too large: {len(result)} > {MAX_TOOL_RESULT_BYTES}")


def _content_is_redundant(content: list[ResultContent], sc: ToolOutputData) -> bool:
    if len(content) != 1:
        return False
    block = content[0]
    if not isinstance(block, TextContent):
        return False
    try:
        return bool(json.loads(block.text) == sc)
    except json.JSONDecodeError, AttributeError:
        return False


def _tool_result_to_openai(result: ToolResult) -> str:
    """Encode a tool result as the Responses API string output; prefix errors with `ERROR`."""
    sc = result.structured_content
    content = list(result.content) if result.content else []
    is_error = result.is_error

    if sc is not None and (not content or _content_is_redundant(content, sc)):
        json_str = pydantic_core.to_json(sc, fallback=str).decode("utf-8")
        _check_size(json_str)
        if is_error:
            return f"ERROR\n{json_str}"
        return json_str

    if content:
        parts = []
        if is_error:
            parts.append("ERROR")
        for block in content:
            if isinstance(block, TextContent):
                parts.append(block.text)
            elif isinstance(block, ImageContent):
                raise NotImplementedError(
                    f"Image content ({block.mime_type}) cannot be serialized as a function call output string"
                )
        text = "\n".join(parts)
        _check_size(text)
        return text

    return "ERROR" if is_error else ""


def _openai_to_tool_result(output: str) -> ToolResult:
    structured_content: ToolOutputData | None = None
    with contextlib.suppress(json.JSONDecodeError):
        structured_content = json.loads(output)
    return ToolResult(content=[TextContent(text=output)], structured_content=structured_content, is_error=False)


def _normalize_call_arguments(arguments: str | dict[str, Any] | list[Any] | None) -> str | None:
    if arguments is None or isinstance(arguments, str):
        return arguments
    return pydantic_core.to_json(arguments, fallback=str).decode("utf-8")


def _tool_choice_from_policy(policy: ToolPolicy) -> ToolChoice:
    """Map supported policies to API values; unknown policies raise `TypeError`."""
    if isinstance(policy, RequireAnyTool):
        return "required"
    if isinstance(policy, AllowAnyToolOrTextMessage):
        return "auto"
    raise TypeError(f"Unknown ToolPolicy: {type(policy).__name__}")


type Message = UserMessage | AssistantMessage | SystemMessage
type TranscriptItem = Message | FunctionCallItem | ReasoningItem | ToolCallOutput


def _sanitize_tool_result(result: ToolResult) -> ToolResult:
    """Strip NULs before JSONB event persistence; PostgreSQL JSONB rejects them.

    Keep the warning with the permanent event, and send sanitized data to the API.
    """
    null_count = 0

    def sanitize(value: Any) -> Any:
        nonlocal null_count
        if isinstance(value, str):
            if "\x00" in value:
                null_count += value.count("\x00")
                return value.replace("\x00", "")
            return value
        if isinstance(value, dict):
            return {k: sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    new_content: list[ResultContent] = []
    for block in result.content:
        if isinstance(block, TextContent):
            new_content.append(TextContent(text=sanitize(block.text)))
        else:
            new_content.append(block)

    # Sanitize structured_content if present (use `is not None` — empty list [] is valid)
    new_sc = sanitize(result.structured_content) if result.structured_content is not None else None

    if null_count > 0:
        warning = f"NOTE: {null_count} null byte(s) removed from tool output"
        if new_content and isinstance(new_content[0], TextContent):
            new_content[0] = TextContent(text=f"{warning}\n{new_content[0].text}")
        else:
            new_content.insert(0, TextContent(text=warning))

    return ToolResult(content=new_content, structured_content=new_sc, is_error=result.is_error)


class Agent:
    def __init__(
        self,
        *,
        tool_provider: ToolProvider,
        client: Model,
        reasoning_effort: ReasoningEffort | None = None,
        reasoning_summary: ReasoningSummary | None = None,
        parallel_tool_calls: bool = True,
        handlers: Iterable[BaseHandler],
        tool_policy: ToolPolicy,
        dynamic_instructions: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        """Keep `tool_policy` fixed for the agent's lifetime.

        Use transcript system messages for static context; `dynamic_instructions` fills the separate API field per request.
        """
        self._tool_provider = tool_provider
        self._client = client
        self._parallel_tool_calls = parallel_tool_calls
        self._tool_policy = tool_policy
        self._transcript: list[TranscriptItem] = []
        self._response_count = 0
        self._reasoning_effort = reasoning_effort
        self._reasoning_summary = reasoning_summary
        self._dynamic_instructions = dynamic_instructions
        self.assistant_text_chunks: list[str] = []
        self.pending_function_calls: list[FunctionCallItem] = []
        self.finished: bool = False
        self._handlers = list(handlers)
        if not self._handlers:
            raise ValueError(
                "At least one handler required to control the agent loop. "
                "Without handlers, the agent will loop indefinitely. "
                "Pass a BaseHandler implementation that controls the loop, such as one that "
                "processes tool calls or terminates after a response."
            )

    @property
    def model(self) -> str:
        return self._client.model

    def _extract_text_from_message(self, msg: UserMessage | AssistantMessage) -> str:
        # TODO: Preserve content-part boundaries in handler events; joining loses them and adds newlines.
        content = msg.content or []
        return "\n".join(part.text for part in content)

    def _notify_user_text(self, text: str) -> None:
        evt = UserText(text=text)
        for h in self._handlers:
            h.on_user_text_event(evt)

    def _notify_assistant_text(self, text: str) -> None:
        evt = AssistantText(text=text)
        for h in self._handlers:
            h.on_assistant_text_event(evt)

    def _notify_handlers_for_transcript_item(self, item: TranscriptItem) -> None:
        if isinstance(item, UserMessage):
            text = self._extract_text_from_message(item)
            self._notify_user_text(text)

        elif isinstance(item, AssistantMessage):
            text = self._extract_text_from_message(item)
            self._notify_assistant_text(text)

        elif isinstance(item, FunctionCallItem):
            for h in self._handlers:
                h.on_tool_call_event(ToolCall(name=item.name, args_json=item.arguments, call_id=item.call_id))

        elif isinstance(item, ToolCallOutput):
            for h in self._handlers:
                h.on_tool_result_event(item)

        elif isinstance(item, ReasoningItem):
            for h in self._handlers:
                h.on_reasoning(item)

        elif isinstance(item, SystemMessage):
            text = "\n".join(part.text for part in item.content) if item.content else ""
            evt = SystemText(text=text)
            for h in self._handlers:
                h.on_system_text_event(evt)

        else:
            raise TypeError(f"Unhandled transcript item type: {type(item).__name__}")

    def process_message(self, message: Message) -> None:
        self._transcript.append(message)
        self._notify_handlers_for_transcript_item(message)

    async def _build_effective_instructions(self) -> str | None:
        """Return dynamic instructions separately from transcript system messages."""
        if self._dynamic_instructions is not None:
            return await self._dynamic_instructions()
        return None

    async def compact_transcript(
        self, *, keep_recent_turns: int = 10, summarization_prompt: str | None = None
    ) -> CompactionResult:
        """Summarize the transcript prefix before the last `keep_recent_turns` items.

        The prefix needs at least three items; it becomes one summary `UserMessage`.
        If no prompt is supplied, load `compaction_prompt.md`.
        """

        boundary_index = max(0, len(self._transcript) - keep_recent_turns)

        if boundary_index < 1:
            return CompactionResult(compacted=False)

        all_to_compact = self._transcript[:boundary_index]
        recent_region = self._transcript[boundary_index:]

        if len(all_to_compact) < 3:
            return CompactionResult(compacted=False)

        summary_text = await self._generate_summary(all_to_compact, summarization_prompt)

        summary_msg = UserMessage.text(summary_text)

        self._transcript = [
            summary_msg,
            *recent_region,  # Keep recent transcript items unchanged.
        ]

        return CompactionResult(compacted=True)

    async def _generate_summary(self, items: list[TranscriptItem], custom_prompt: str | None) -> str:
        """Serialize messages, reasoning items, function calls, and outputs for the summarizer."""
        # TODO: Consider stripping/formatting to remove fields without semantic content
        #  (e.g., tool call IDs, encrypted reasoning data, internal metadata)
        adapter = TypeAdapter(list[TranscriptItem])
        conversation = adapter.dump_json(items, exclude_none=True, indent=2).decode()

        if custom_prompt is None:
            prompt_file = Path(__file__).parent / "compaction_prompt.md"
            custom_prompt = prompt_file.read_text()

        req = ResponsesRequest(input=[UserMessage.text(conversation)], instructions=custom_prompt, stream=False)

        resp = await self._client.sample(self._client.prepare(req))

        if resp.output and len(resp.output) > 0:
            first = resp.output[0]
            if isinstance(first, AssistantMessageOut):
                return first.text

        raise RuntimeError("Summary generation failed: LLM response missing assistant message")

    async def step(self) -> StepResult:
        """Run one step and resolve pending calls.

        Preserves accumulated text and resets `finished` before returning; output is emitted to handlers.
        """
        await self._run_one_phase()
        if self.pending_function_calls:
            await self._handle_pending_tool_calls()
        done = self.finished
        self.finished = False  # Reset so step() can be called again
        return StepResult(done=done)

    async def run(self) -> str:
        """Clear state, then step until a handler finishes and return the assembled text."""
        self.assistant_text_chunks.clear()
        self.pending_function_calls.clear()
        self.finished = False
        try:
            while True:
                result = await self.step()
                if result.done:
                    break
            return "\n".join(self.assistant_text_chunks)
        except Exception as exc:
            for h in self._handlers:
                h.on_error(exc)
            raise

    async def _handle_pending_tool_calls(self) -> None:
        function_calls: list[FunctionCallItem] = list(self.pending_function_calls)
        calls: list[tuple[FunctionCallItem, str | None]] = [
            (function_call, _normalize_call_arguments(function_call.arguments)) for function_call in function_calls
        ]

        local_result_map: dict[str, ToolResult] = {
            evt.call_id: evt.result for evt in self._transcript if isinstance(evt, ToolCallOutput)
        }

        async def _invoke(
            function_call: FunctionCallItem, args_json: str | None, local_map: dict[str, ToolResult] = local_result_map
        ) -> ToolResult:
            call_id = _require_call_id(function_call)
            # No agent-level before-tool gating; Policy Gateway middleware enforces approvals/denials
            if call_id in local_map:
                # Replay persisted output by call_id.
                return local_map[call_id]

            try:
                parsed = json.loads(args_json) if args_json else {}
            except json.JSONDecodeError as e:
                return ToolResult.error(f"Invalid JSON in tool arguments: {e!s}")
            if not isinstance(parsed, dict):
                return ToolResult.error(f"Tool arguments must be a JSON object, got {type(parsed).__name__}")
            args: dict[str, Any] = parsed
            # Providers map expected failures to ToolResult.error(); unexpected exceptions propagate.
            return await self._tool_provider.call_tool(function_call.name, args)

        if self._parallel_tool_calls:
            await self._run_tool_calls_parallel(calls, function_calls, _invoke)
        else:
            await self._run_tool_calls_sequential(calls, _invoke)
        self.pending_function_calls.clear()

    async def _run_tool_calls_parallel(
        self, calls: list[tuple[FunctionCallItem, str | None]], function_calls: list[FunctionCallItem], invoker
    ) -> None:
        results: dict[str, ToolResult] = {}

        async with anyio.create_task_group() as tg:

            async def runner(fc: FunctionCallItem, aj: str | None) -> None:
                call_id = _require_call_id(fc)
                results[call_id] = await invoker(fc, aj)

            for function_call, args_json in calls:
                tg.start_soon(runner, function_call, args_json)

        for function_call in function_calls:
            call_id = _require_call_id(function_call)
            if call_id not in results:
                raise RuntimeError(f"Missing tool output for call_id={call_id!r}")
            self._emit_tool_result(function_call, results[call_id])

    async def _run_tool_calls_sequential(self, calls: list[tuple[FunctionCallItem, str | None]], invoker) -> None:
        for function_call, args_json in calls:
            self._emit_tool_result(function_call, await invoker(function_call, args_json))

    def to_openai_messages(self) -> list[InputItem]:
        """Serialize the complete transcript on each stateless Responses request.

        Forward SDK reasoning items unchanged and in order; never synthesize `rs_`/`fc_` IDs.
        """
        items: list[InputItem] = []
        for item in self._transcript:
            if isinstance(item, UserMessage | AssistantMessage | SystemMessage):
                # Pydantic deepcopy hangs under pytest-xdist -n16; it works in isolation, and the cause is unknown.
                item_type = type(item)
                items.append(item_type.model_validate_json(item.model_dump_json()))
                continue
            if isinstance(item, ReasoningItem):
                items.append(item)
                continue
            if isinstance(item, FunctionCallItem):
                items.append(item)
                continue
            if isinstance(item, ToolCallOutput):
                items.append(FunctionCallOutputItem(call_id=item.call_id, output=_tool_result_to_openai(item.result)))
                continue
            raise TypeError(f"Unsupported transcript item for OpenAI input: {type(item)}")
        return items

    async def _run_one_phase(self):
        # Poll handlers in order; the first non-NoAction decision wins.
        decision: LoopDecision = NoAction()
        for h in self._handlers:
            d = h.on_before_sample()
            if not isinstance(d, NoAction):
                decision = d
                break

        if isinstance(decision, Abort):
            self.finished = True
            return
        if isinstance(decision, Compact):
            # ReasoningItems cannot be reused outside their original response context,
            # so compaction must not be triggered if the last transcript item is a ReasoningItem
            if self._transcript and isinstance(self._transcript[-1], ReasoningItem):
                raise RuntimeError(
                    "Cannot compact transcript when last item is a ReasoningItem. "
                    "Handlers must not return Compact() after receiving reasoning output."
                )
            result = await self.compact_transcript(keep_recent_turns=decision.keep_recent_turns)
            if result.compacted:
                logger.info(
                    "Transcript compacted (kept %d recent turns, compacted %d items)",
                    decision.keep_recent_turns,
                    len(self._transcript) - decision.keep_recent_turns - 1,  # -1 for summary message
                )
            else:
                logger.info("Compaction skipped (not enough items to compact)")

            for h in self._handlers:
                h.on_compaction_complete(compacted=result.compacted)

            return  # Continue to next iteration after compaction

        if isinstance(decision, InjectItems):
            for item in decision.items:
                self._transcript.append(item)
                self._notify_handlers_for_transcript_item(item)

                if isinstance(item, FunctionCallItem):
                    self.pending_function_calls.append(item)

            # Let step() resolve injected calls before another sample.
            return

        resp_output: list[ReasoningItem | FunctionCallItem | FunctionCallOutputItem | AssistantMessageOut] | None = None

        should_sample_llm = False

        if isinstance(decision, NoAction):
            should_sample_llm = True
        else:
            raise TypeError(f"Unsupported loop decision: {type(decision).__name__}")

        if should_sample_llm:
            tool_choice = _tool_choice_from_policy(self._tool_policy)
            reasoning_param = build_reasoning_params(self._reasoning_effort, self._reasoning_summary)
            tool_schemas = await self._tool_provider.list_tools()

            req = ResponsesRequest(
                input=self.to_openai_messages(),
                instructions=await self._build_effective_instructions(),
                stream=False,
                tool_choice=tool_choice,
                store=True,
                parallel_tool_calls=self._parallel_tool_calls,
                tools=[_make_strict_function_tool(t.name, t.description, t.input_schema) for t in tool_schemas],
                reasoning=reasoning_param,
            )

            # TODO: Avoid O(N²) full-transcript snapshots; store non-derivable request data and reconstruct
            # each request from the transcript and metadata.
            request_id = uuid4()
            phase_number = self._response_count
            prepared_request = self._client.prepare(req)
            for h in self._handlers:
                h.on_api_request_event(
                    ApiRequest(
                        api_shape=self._client.api_shape,
                        request=prepared_request,
                        model=self._client.model,
                        request_id=request_id,
                        phase_number=phase_number,
                    )
                )

            resp = await self._client.sample(prepared_request)
            sdk_usage = resp.usage
            usage = (
                GroundTruthUsage(
                    model=self._client.model,
                    input_tokens=sdk_usage.input_tokens,
                    input_tokens_details=sdk_usage.input_tokens_details,
                    output_tokens=sdk_usage.output_tokens,
                    output_tokens_details=sdk_usage.output_tokens_details,
                    total_tokens=sdk_usage.total_tokens,
                )
                if sdk_usage is not None
                else GroundTruthUsage(model=self._client.model)
            )
            resp_output = resp.output
            if resp_output is not None:
                self._process_resp_output(resp_output)
            for h in self._handlers:
                h.on_response(
                    Response(response_id=resp.id, request_id=request_id, usage=usage, model=self._client.model)
                )
            self._response_count += 1
        # Handlers end the loop; tool policies can prevent text-only responses.

    def _process_resp_output(
        self, resp_output: Sequence[ReasoningItem | FunctionCallItem | FunctionCallOutputItem | AssistantMessageOut]
    ) -> None:
        self.pending_function_calls.clear()
        # Skip transcript items already seen by ID.
        existing_ids: set[str] = set()
        for evt in self._transcript:
            if isinstance(evt, ReasoningItem | FunctionCallItem):
                eid = evt.id
                if isinstance(eid, str) and eid:
                    existing_ids.add(eid)
        handled_call_ids = {evt.call_id for evt in self._transcript if isinstance(evt, ToolCallOutput)}
        for item in resp_output:
            iid = item.id if isinstance(item, ReasoningItem | FunctionCallItem) else None
            if isinstance(iid, str) and iid in existing_ids:
                continue
            if isinstance(item, ReasoningItem):
                self._transcript.append(item)
                self._notify_handlers_for_transcript_item(item)

            elif isinstance(item, AssistantMessageOut):
                self.assistant_text_chunks.append(item.text)
                assistant_msg = item.to_input_item()
                self._transcript.append(assistant_msg)
                self._notify_handlers_for_transcript_item(assistant_msg)

            elif isinstance(item, FunctionCallOutputItem):
                if item.output is None:
                    raise ValueError("FunctionCallOutputItem.output is None")
                result = _openai_to_tool_result(item.output)
                original_call_id = item.call_id
                if not isinstance(original_call_id, str) or not original_call_id:
                    raise ValueError(f"FunctionCallOutputItem has invalid call_id: {original_call_id!r}")
                tool_output = ToolCallOutput(call_id=original_call_id, result=result)
                handled_call_ids.add(original_call_id)
                self._transcript.append(tool_output)
                self._notify_handlers_for_transcript_item(tool_output)
                if self.pending_function_calls:
                    self.pending_function_calls = [
                        fc for fc in self.pending_function_calls if fc.call_id != original_call_id
                    ]

            elif isinstance(item, FunctionCallItem):
                call_id = _require_call_id(item)
                self._transcript.append(item)
                self._notify_handlers_for_transcript_item(item)
                if call_id in handled_call_ids:
                    continue
                self.pending_function_calls.append(item)
            else:
                raise TypeError(f"Unsupported Responses output item: {type(item)}")

    def _emit_tool_result(self, function_call: FunctionCallItem, result: ToolResult) -> None:
        call_id = _require_call_id(function_call)
        sanitized = _sanitize_tool_result(result)
        event = ToolCallOutput(call_id=call_id, result=sanitized)
        self._transcript.append(event)
        for h in self._handlers:
            h.on_tool_result_event(event)
