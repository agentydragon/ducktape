from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable, Sequence
from typing import Any

import pydantic_core

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
from skills.info_gathering.evals.agent_runtime.events import AssistantText, ToolCall, ToolCallOutput
from skills.info_gathering.evals.agent_runtime.handler import BaseHandler
from skills.info_gathering.evals.agent_runtime.loop_control import AllowAnyToolOrTextMessage, RequireAnyTool, ToolPolicy
from skills.info_gathering.evals.agent_runtime.tool_provider import (
    ImageContent,
    ResultContent,
    TextContent,
    ToolOutputData,
    ToolProvider,
    ToolResult,
)


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


type Message = UserMessage | SystemMessage
type TranscriptItem = Message | AssistantMessage | FunctionCallItem | ReasoningItem | ToolCallOutput


def _sanitize_tool_result(result: ToolResult) -> ToolResult:
    """Strip NUL bytes from the result's text and structured content, and prepend a note counting them."""
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
        self, *, tool_provider: ToolProvider, client: Model, handlers: Iterable[BaseHandler], tool_policy: ToolPolicy
    ) -> None:
        """Keep `tool_policy` fixed for the agent's lifetime."""
        self._tool_provider = tool_provider
        self._client = client
        self._tool_policy = tool_policy
        self._transcript: list[TranscriptItem] = []
        self.pending_function_calls: list[FunctionCallItem] = []
        self._handlers = list(handlers)

    def _extract_text_from_message(self, msg: AssistantMessage) -> str:
        # TODO: Preserve content-part boundaries in handler events; joining loses them and adds newlines.
        content = msg.content or []
        return "\n".join(part.text for part in content)

    def _notify_assistant_text(self, text: str) -> None:
        evt = AssistantText(text=text)
        for h in self._handlers:
            h.on_assistant_text_event(evt)

    def process_message(self, message: Message) -> None:
        self._transcript.append(message)

    async def step(self) -> None:
        """Sample once and resolve pending tool calls."""
        await self._run_one_phase()
        if self.pending_function_calls:
            await self._handle_pending_tool_calls()

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
            # No before-tool approval gating: every call reaches the tool provider.
            if call_id in local_map:
                # Replay the output already recorded in the transcript for this call_id.
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

        for function_call, args_json in calls:
            self._emit_tool_result(function_call, await _invoke(function_call, args_json))
        self.pending_function_calls.clear()

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
        tool_choice = _tool_choice_from_policy(self._tool_policy)
        tool_schemas = await self._tool_provider.list_tools()

        req = ResponsesRequest(
            input=self.to_openai_messages(),
            stream=False,
            tool_choice=tool_choice,
            store=True,
            parallel_tool_calls=False,
            tools=[_make_strict_function_tool(t.name, t.description, t.input_schema) for t in tool_schemas],
        )

        resp = await self._client.sample(self._client.prepare(req))
        if resp.output is not None:
            self._process_resp_output(resp.output)
        for h in self._handlers:
            h.on_response(self._client.model)

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

            elif isinstance(item, AssistantMessageOut):
                assistant_msg = item.to_input_item()
                self._transcript.append(assistant_msg)
                self._notify_assistant_text(self._extract_text_from_message(assistant_msg))

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
                if self.pending_function_calls:
                    self.pending_function_calls = [
                        fc for fc in self.pending_function_calls if fc.call_id != original_call_id
                    ]

            elif isinstance(item, FunctionCallItem):
                call_id = _require_call_id(item)
                self._transcript.append(item)
                for h in self._handlers:
                    h.on_tool_call_event(ToolCall(name=item.name, args_json=item.arguments, call_id=call_id))
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
