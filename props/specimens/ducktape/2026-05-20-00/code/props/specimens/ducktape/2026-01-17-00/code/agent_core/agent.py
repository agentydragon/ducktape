"""Agent on OpenAI Responses API with MCP tool wiring.

For stateless reasoning/tool replay demo, see :/adgn/examples/openai_api/stateless_two_step_demo.py
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import anyio
import pydantic_core
from fastmcp.client import Client
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from mcp import types as mcp_types
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
from agent_core.loop_control import (
    Abort,
    AllowAnyToolOrTextMessage,
    Compact,
    ForbidAllTools,
    InjectItems,
    LoopDecision,
    NoAction,
    RequireAnyTool,
    RequireSpecific,
    ToolPolicy,
)
from openai_utils.model import (
    AssistantMessage,
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    FunctionCallOutputType,
    FunctionOutputImageContent,
    FunctionOutputTextContent,
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
from openai_utils.types import ReasoningEffort, ReasoningSummary, build_reasoning_params

from .handler import BaseHandler

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


@dataclass
class AgentResult:
    text: str
    # NOTE: We intentionally do NOT return transcript/events in agent result.
    # Tests or callers that need access to the event sequence should register a handler
    # (e.g. a test-only RecordingHandler) and pass it via `handlers` argument to Agent.create().


@dataclass
class CompactionResult:
    """Result of transcript compaction operation."""

    compacted: bool


@dataclass(slots=True)
class ToolCallOutcome:
    """Tool invocation result.

    Fields:
    - result: The FastMCP CallToolResult
    - was_aborted: True if execution was aborted (policy denial), False otherwise
    """

    result: CallToolResult
    was_aborted: bool = False


def _validate_schema_strict_compatible(schema: dict[str, Any], tool_name: str) -> None:
    """Validate that a JSON schema is compatible with OpenAI's strict mode.

    Strict mode requirements for objects:
    1. Regular objects with fixed properties: must have "additionalProperties": false and all properties in "required"
    2. Map objects (dicts): may use "patternProperties" or "additionalProperties" with a schema (not false)

    Recursively checks schema structure for OpenAI strict mode compatibility.

    Raises RuntimeError with specific details if schema is not compatible.
    """

    def check_subschema(subschema: dict[str, Any], path: str) -> None:
        """Check a single subschema for strict mode compatibility."""
        schema_type = subschema.get("type")

        # Check object types
        if schema_type == "object" or (isinstance(schema_type, list) and "object" in schema_type):
            # OpenAI strict mode: all objects must have additionalProperties: false
            # No exemptions for patternProperties or map-like objects
            if "patternProperties" in subschema:
                raise RuntimeError(
                    f"Tool '{tool_name}' at {path}: object uses 'patternProperties' which is not allowed in strict mode. "
                    f"Refactor dict[str, T] fields to list[{{key: str, value: T}}] or similar fixed structure."
                )

            if "additionalProperties" not in subschema:
                raise RuntimeError(
                    f"Tool '{tool_name}' at {path}: object missing 'additionalProperties'. "
                    f"Add 'model_config = ConfigDict(extra=\"forbid\")' to the Pydantic model."
                )

            additional_props = subschema.get("additionalProperties")
            if additional_props is not False:
                raise RuntimeError(
                    f"Tool '{tool_name}' at {path}: 'additionalProperties' must be false, got {additional_props}. "
                    f"If this is a dict field, refactor to a list of objects with fixed schema."
                )

            # Check required properties
            properties = subschema.get("properties", {})
            required = set(subschema.get("required", []))
            if properties and set(properties.keys()) != required:
                missing = set(properties.keys()) - required
                raise RuntimeError(
                    f"Tool '{tool_name}' at {path}: all properties must be in 'required'. "
                    f"Missing: {missing}. "
                    f"Fix: Remove Field(default=...) or Field(default_factory=...) from Pydantic model."
                )

            # Recursively check nested properties
            for prop_name, prop_schema in properties.items():
                check_subschema(prop_schema, f"{path}.properties.{prop_name}")

        # Check array items
        if schema_type == "array" or (isinstance(schema_type, list) and "array" in schema_type):
            items = subschema.get("items")
            if items and isinstance(items, dict):
                check_subschema(items, f"{path}.items")

        # Check combinators
        for key in ["anyOf", "oneOf", "allOf"]:
            variants = subschema.get(key)
            if isinstance(variants, list):
                for i, variant in enumerate(variants):
                    if isinstance(variant, dict):
                        check_subschema(variant, f"{path}.{key}[{i}]")

    # Start validation from root
    check_subschema(schema, "$")

    # Check $defs (definitions)
    defs = schema.get("$defs", {})
    for def_name, def_schema in defs.items():
        if isinstance(def_schema, dict):
            check_subschema(def_schema, f"$defs.{def_name}")


def _make_strict_function_tool(tool_name: str, description: str, input_schema: dict[str, Any]) -> FunctionToolParam:
    """Create a FunctionToolParam with strict mode after validating schema compatibility.

    Args:
        tool_name: Name of the tool
        description: Tool description
        input_schema: JSON schema for tool input (must be strict-compatible)

    Returns:
        FunctionToolParam configured with strict=True

    Raises:
        RuntimeError: If schema is not compatible with OpenAI's strict mode
    """
    _validate_schema_strict_compatible(input_schema, tool_name)
    return FunctionToolParam(name=tool_name, description=description, parameters=input_schema, strict=True)


def _require_call_id(function_call: FunctionCallItem) -> str:
    call_id = function_call.call_id
    if not call_id:
        raise RuntimeError("FunctionCallItem missing call_id")
    return call_id


DEFAULT_ABORT_ERROR = "tool execution aborted"


def _abort_result(reason: str = DEFAULT_ABORT_ERROR) -> CallToolResult:
    """Return an error result for aborted tool calls."""
    return CallToolResult(
        content=[mcp_types.TextContent(type="text", text=reason)], structured_content=None, meta=None, is_error=True
    )


# Size limits (bytes)
MAX_TOOL_RESULT_BYTES = 10 * 1024 * 1024  # 10 MiB

_ERROR_PREFIX = FunctionOutputTextContent(text="ERROR")

# Data URL format constants
_DATA_URL_PREFIX = "data:"
_BASE64_SEPARATOR = ";base64,"


def _parse_data_url(url: str) -> tuple[str, str] | None:
    """Parse a data URL into (mime_type, base64_data). Returns None if invalid."""
    if not url.startswith(_DATA_URL_PREFIX) or _BASE64_SEPARATOR not in url:
        return None
    header, data = url.split(_BASE64_SEPARATOR, 1)
    mime_type = header.removeprefix(_DATA_URL_PREFIX)
    return (mime_type, data)


def _make_data_url(mime_type: str, base64_data: str) -> str:
    """Create a data URL from mime type and base64 data."""
    return f"{_DATA_URL_PREFIX}{mime_type}{_BASE64_SEPARATOR}{base64_data}"


def _check_size(result: str) -> None:
    if len(result) > MAX_TOOL_RESULT_BYTES:
        raise RuntimeError(f"Tool output too large: {len(result)} > {MAX_TOOL_RESULT_BYTES}")


def _content_is_redundant(
    content: list[mcp_types.TextContent | mcp_types.ImageContent | Any], sc: dict[str, Any]
) -> bool:
    """Check if content is just JSON serialization of structuredContent."""
    if len(content) != 1:
        return False
    block = content[0]
    if not isinstance(block, mcp_types.TextContent):
        return False
    try:
        return bool(json.loads(block.text) == sc)
    except (json.JSONDecodeError, AttributeError):
        return False


def _content_block_to_openai(
    block: mcp_types.TextContent | mcp_types.ImageContent | Any,
) -> FunctionOutputTextContent | FunctionOutputImageContent:
    if isinstance(block, mcp_types.TextContent):
        return FunctionOutputTextContent(text=block.text)
    if isinstance(block, mcp_types.ImageContent):
        return FunctionOutputImageContent(image_url=_make_data_url(block.mimeType, block.data))
    # TODO: Wire AudioContent when OpenAI Responses API supports it
    raise NotImplementedError(f"Unsupported MCP content type: {type(block).__name__}")


def _call_tool_result_to_openai(result: mcp_types.CallToolResult) -> FunctionCallOutputType:
    """Convert mcp.types.CallToolResult to OpenAI output format.

    When isError=True, always returns a list with "ERROR" prefix block.
    """
    sc = result.structuredContent
    content = list(result.content) if result.content else []
    is_error = bool(result.isError)

    # Case 1: structuredContent present, content empty or redundant
    if sc is not None and (not content or _content_is_redundant(content, sc)):
        json_str = pydantic_core.to_json(sc, fallback=str).decode("utf-8")
        _check_size(json_str)
        if is_error:
            return [_ERROR_PREFIX, FunctionOutputTextContent(text=json_str)]
        return json_str

    # Case 2: Content blocks present
    if content:
        items = [_content_block_to_openai(block) for block in content]
        if is_error:
            items.insert(0, _ERROR_PREFIX)
        return items

    # Case 3: Empty
    if is_error:
        return [_ERROR_PREFIX]
    return ""


def _image_url_to_mcp_content(image_url: str | None) -> mcp_types.ImageContent:
    """Convert a data URL to MCP ImageContent."""
    if not isinstance(image_url, str):
        raise ValueError(f"image_url must be a string, got {type(image_url)}")
    parsed = _parse_data_url(image_url)
    if parsed is None:
        url_preview = image_url[:50] if image_url else "None"
        raise ValueError(f"Unsupported image_url format: {url_preview}...")
    mime_type, data = parsed
    return mcp_types.ImageContent(type="image", mimeType=mime_type, data=data)


def _openai_to_mcp_result(output: FunctionCallOutputType) -> mcp_types.CallToolResult:
    """Convert OpenAI FunctionCallOutputItem.output format to mcp.types.CallToolResult."""
    content: list[
        mcp_types.TextContent
        | mcp_types.ImageContent
        | mcp_types.AudioContent
        | mcp_types.ResourceLink
        | mcp_types.EmbeddedResource
    ] = []
    structured_content: dict[str, Any] | None = None

    if isinstance(output, str):
        # Try to parse as JSON for structuredContent
        with contextlib.suppress(json.JSONDecodeError):
            structured_content = json.loads(output)
        content.append(mcp_types.TextContent(type="text", text=output))
    else:
        for item in output:
            if isinstance(item, FunctionOutputTextContent):
                content.append(mcp_types.TextContent(type="text", text=item.text))
            elif isinstance(item, FunctionOutputImageContent):
                content.append(_image_url_to_mcp_content(item.image_url))
            else:
                raise ValueError(f"Unsupported content type in tool output: {type(item)}")

    return mcp_types.CallToolResult(content=content, structuredContent=structured_content, isError=False)


def _fastmcp_to_mcp_result(res: CallToolResult) -> mcp_types.CallToolResult:
    """Convert a FastMCP CallToolResult to mcp.types.CallToolResult.

    Builds a minimal payload with alias field names (structuredContent, isError).
    """
    payload: dict[str, Any] = {"isError": bool(res.is_error)}
    if res.structured_content is not None:
        payload["structuredContent"] = res.structured_content
    payload["content"] = list(res.content or [])
    return mcp_types.CallToolResult.model_validate(payload)


def _normalize_call_arguments(arguments: str | dict[str, Any] | list[Any] | None) -> str | None:
    """Normalize function call arguments to JSON string."""
    if arguments is None or isinstance(arguments, str):
        return arguments
    return pydantic_core.to_json(arguments, fallback=str).decode("utf-8")


SYSTEM_INSTRUCTIONS = "You are a code agent. Be concise."


def _tool_choice_from_policy(policy: ToolPolicy) -> ToolChoice:
    """Map a ToolPolicy to Responses API tool_choice value.

    Exhaustive and strict: raises on unknown policy; RequireSpecific supports exactly one name.
    """
    if isinstance(policy, RequireAnyTool):
        return "required"
    if isinstance(policy, AllowAnyToolOrTextMessage):
        return "auto"
    if isinstance(policy, ForbidAllTools):
        return "none"
    if isinstance(policy, RequireSpecific):
        if len(policy.names) == 1:
            return ToolChoiceFunction(name=policy.names[0])
        raise ValueError("RequireSpecific with multiple names is not supported for Responses.tool_choice")
    raise TypeError(f"Unknown ToolPolicy: {type(policy).__name__}")


type Message = UserMessage | AssistantMessage | SystemMessage
type TranscriptItem = Message | FunctionCallItem | ReasoningItem | ToolCallOutput


def _sanitize_mcp_result(result: mcp_types.CallToolResult) -> mcp_types.CallToolResult:
    """Remove null bytes from MCP CallToolResult, prepending warning if any found.

    Why here (not at OpenAI conversion): PostgreSQL JSONB doesn't support null bytes,
    and events are persisted via Pydantic model_dump(). Tools like `rg -0` produce null
    bytes that would break persistence. Sanitizing at event creation ensures:
    1. Stored events are JSONB-safe
    2. Warning message is part of the permanent record
    3. OpenAI API (via _call_tool_result_to_openai) receives clean data
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

    # Sanitize content blocks
    new_content: list[
        mcp_types.TextContent
        | mcp_types.ImageContent
        | mcp_types.AudioContent
        | mcp_types.ResourceLink
        | mcp_types.EmbeddedResource
    ] = []
    for block in result.content:
        if isinstance(block, mcp_types.TextContent):
            new_content.append(mcp_types.TextContent(type="text", text=sanitize(block.text)))
        else:
            # Other content types (ImageContent, AudioContent, etc.) pass through
            new_content.append(block)

    # Sanitize structuredContent if present
    new_sc = sanitize(result.structuredContent) if result.structuredContent else None

    # Prepend warning if null bytes were removed
    if null_count > 0:
        warning = f"NOTE: {null_count} null byte(s) removed from tool output"
        # If first block is text, prepend warning to it; otherwise insert new block
        if new_content and isinstance(new_content[0], mcp_types.TextContent):
            new_content[0] = mcp_types.TextContent(type="text", text=f"{warning}\n{new_content[0].text}")
        else:
            new_content.insert(0, mcp_types.TextContent(type="text", text=warning))

    return mcp_types.CallToolResult(
        content=new_content, structuredContent=new_sc, isError=result.isError, _meta=result.meta
    )


class Agent:
    def __init__(
        self,
        *,
        mcp_client: Client,
        client: OpenAIModelProto,
        reasoning_effort: ReasoningEffort | None = None,
        reasoning_summary: ReasoningSummary | None = None,
        parallel_tool_calls: bool,
        handlers: Iterable[BaseHandler],
        tool_policy: ToolPolicy,
        dynamic_instructions: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._mcp_client = mcp_client
        self._client = client
        self._parallel_tool_calls = parallel_tool_calls
        self._tool_policy = tool_policy
        self._transcript: list[TranscriptItem] = []
        self._reasoning_effort = reasoning_effort
        self._reasoning_summary = reasoning_summary
        self._dynamic_instructions = dynamic_instructions
        # Agent state fields
        self.assistant_text_chunks: list[str] = []
        self.pending_function_calls: list[FunctionCallItem] = []
        self.finished: bool = False
        # Track function calls for debugging
        self._function_call_map: dict[str, FunctionCallItem] = {}
        # Handler list for event notification and loop control
        self._handlers = list(handlers)
        if not self._handlers:
            raise ValueError(
                "At least one handler required to control the agent loop. "
                "Without handlers, the agent will loop indefinitely. "
                "Add a handler:\n"
                "  • DisplayEventsHandler() - for console output\n"
                "  • TranscriptHandler(events_path=...) - for logging\n"
                "  • AbortIf(lambda: should_stop) - to abort when condition is met\n"
                "  • SequenceHandler([...]) - for fixed action sequences\n"
                "  • Custom handler - subclass BaseHandler for specialized control"
            )

    @property
    def model(self) -> str:
        """Get the model name used by this agent."""
        return self._client.model

    def _extract_text_from_message(self, msg: UserMessage | AssistantMessage) -> str:
        """Extract text content from message's content parts.

        TODO: This is lossy - joins multiple content part objects with "\n" separator,
        losing part boundaries and adding separators that weren't in the original OpenAI response.
        Should preserve the multi-part structure faithfully in handler events.
        """
        # UserMessage.content is always present (list[InputTextPart])
        # AssistantMessage.content can be None or list[OutputTextPart]
        content = msg.content if msg.content else []
        return "\n".join(part.text for part in content)

    def _notify_user_text(self, text: str) -> None:
        """Notify all handlers of a user text event."""
        evt = UserText(text=text)
        for h in self._handlers:
            h.on_user_text_event(evt)

    def _notify_assistant_text(self, text: str) -> None:
        """Notify all handlers of an assistant text event."""
        evt = AssistantText(text=text)
        for h in self._handlers:
            h.on_assistant_text_event(evt)

    def _notify_handlers_for_transcript_item(self, item: TranscriptItem) -> None:
        """Dispatch handler notifications based on transcript item type.

        This is the single source of truth for "what handler events does this item type trigger".
        Called for both injected items and items added from API responses.
        """
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
            # Extract text from SystemMessage content parts
            text = "\n".join(part.text for part in item.content) if item.content else ""
            evt = SystemText(text=text)
            for h in self._handlers:
                h.on_system_text_event(evt)

        else:
            raise AssertionError(f"Unhandled transcript item type: {type(item).__name__}")

    def process_message(self, message: Message) -> None:
        """Add a message to the transcript and notify handlers.

        Use this to set up initial context (system prompts, user messages) before calling run().

        Args:
            message: A UserMessage, AssistantMessage, or SystemMessage to add to transcript
        """
        self._transcript.append(message)
        self._notify_handlers_for_transcript_item(message)

    # TODO: Consider eliminating these no-handler methods by allowing handlers to be attached
    # after transcript reconstruction. Then all inserts could use process_message() uniformly.
    # Current use case: session resume / conversation replay (cmd_speak_with_dead.py).

    def insert_transcript_item(self, item: TranscriptItem) -> None:
        """Insert a transcript item (message, tool call, reasoning, or tool output) without triggering handlers.

        Use this to reconstruct a full transcript including tool calls and their outputs,
        e.g., when resuming from a saved session or replaying a previous conversation.

        Unlike process_message(), handlers are NOT notified.

        Args:
            item: A TranscriptItem (Message, FunctionCallItem, ReasoningItem, or ToolCallOutput)
        """
        self._transcript.append(item)

    def insert_transcript_items(self, items: Sequence[TranscriptItem]) -> None:
        """Insert multiple transcript items without triggering handlers.

        Convenience method for bulk insertion when reconstructing a transcript.
        Equivalent to calling insert_transcript_item() for each item.

        Args:
            items: Sequence of TranscriptItem to add
        """
        self._transcript.extend(items)

    async def _build_effective_instructions(self) -> str | None:
        """Build instructions for the OpenAI API request.

        Returns result of dynamic_instructions callback if provided, otherwise None.
        Note: This is the 'instructions' field in the API, separate from system messages
        in the transcript.
        """
        if self._dynamic_instructions is not None:
            return await self._dynamic_instructions()
        return None

    async def compact_transcript(
        self, *, keep_recent_turns: int = 10, summarization_prompt: str | None = None
    ) -> CompactionResult:
        """Compact old conversation by summarizing.

        Preserves:
        - Recent N transcript items

        Compacts:
        - Old UserMessage/AssistantMessage text
        - Old FunctionCallItem/ToolCallOutput (tool call chains)
        - Old ReasoningItem blocks

        Returns summary as a single UserMessage inserted before recent turns.

        Args:
            keep_recent_turns: Number of recent transcript items to preserve
            summarization_prompt: Custom prompt for summarization (default: load from file)

        Returns:
            CompactionResult with statistics about what was compacted
        """

        # Find boundary: keep last N items, compact everything before
        boundary_index = max(0, len(self._transcript) - keep_recent_turns)

        if boundary_index < 1:
            return CompactionResult(compacted=False)

        # Partition transcript in original order
        all_to_compact = self._transcript[:boundary_index]
        recent_region = self._transcript[boundary_index:]

        # Check if we have enough items to make compaction worthwhile
        if len(all_to_compact) < 3:
            return CompactionResult(compacted=False)

        # Generate summary via LLM
        summary_text = await self._generate_summary(all_to_compact, summarization_prompt)

        # Rebuild transcript
        summary_msg = UserMessage.text(summary_text)

        self._transcript = [
            summary_msg,  # Summary of compacted conversation
            *recent_region,  # Recent turns preserved verbatim (no ReasoningItems)
        ]

        return CompactionResult(compacted=True)

    async def _generate_summary(self, items: list[TranscriptItem], custom_prompt: str | None) -> str:
        """Use LLM to summarize transcript items.

        Handles:
        - UserMessage: full text
        - AssistantMessage: full text
        - ReasoningItem: summary only (not full extended thinking)
        - FunctionCallItem: tool name and args
        - ToolCallOutput: result summary
        """

        # Serialize transcript items to JSON using TypeAdapter
        # TODO: Consider stripping/formatting to remove fields without semantic content
        #  (e.g., tool call IDs, encrypted reasoning data, internal metadata)
        adapter = TypeAdapter(list[TranscriptItem])
        conversation = adapter.dump_json(items, exclude_none=True, indent=2).decode()

        # Load default prompt from file if not provided
        if custom_prompt is None:
            prompt_file = Path(__file__).parent / "compaction_prompt.md"
            custom_prompt = prompt_file.read_text()

        # Build summarization request
        req = ResponsesRequest(input=[UserMessage.text(conversation)], instructions=custom_prompt, stream=False)

        resp = await self._client.responses_create(req)

        # Extract text from response
        if resp.output and len(resp.output) > 0:
            first = resp.output[0]
            if isinstance(first, AssistantMessageOut):
                return first.text

        raise RuntimeError("Summary generation failed: LLM response missing assistant message")

    async def run(self) -> AgentResult:
        """Run the agent loop until completion.

        Before calling run(), add messages to the transcript using process_message() or insert_messages().
        Example:
            agent.process_message(SystemMessage.text("You are a helpful assistant"))
            agent.process_message(UserMessage.text("Hello"))
            result = await agent.run()
        """
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
            # Forward error to all handlers
            for h in self._handlers:
                h.on_error(exc)
            raise

    async def _handle_pending_tool_calls(self) -> None:
        function_calls: list[FunctionCallItem] = list(self.pending_function_calls)
        calls: list[tuple[FunctionCallItem, str | None]] = [
            (function_call, _normalize_call_arguments(function_call.arguments)) for function_call in function_calls
        ]

        # local_result_map stores mcp_types.CallToolResult (Pydantic) from ToolCallOutput events
        local_result_map: dict[str, mcp_types.CallToolResult] = {
            evt.call_id: evt.result for evt in self._transcript if isinstance(evt, ToolCallOutput)
        }

        async def _invoke(
            function_call: FunctionCallItem,
            args_json: str | None,
            local_map: dict[str, mcp_types.CallToolResult] = local_result_map,
        ) -> ToolCallOutcome:
            call_id = _require_call_id(function_call)
            # No agent-level before-tool gating; Policy Gateway middleware enforces approvals/denials
            if call_id in local_map:
                # Already have a result (replay scenario) - convert MCP Pydantic back to FastMCP dataclass
                mcp_result = local_map[call_id]
                fastmcp_result = CallToolResult(
                    content=list(mcp_result.content),
                    structured_content=mcp_result.structuredContent,
                    meta=mcp_result.meta,
                    is_error=bool(mcp_result.isError),
                )
                return ToolCallOutcome(result=fastmcp_result)

            # Call MCP tool - returns FastMCP CallToolResult (dataclass)
            try:
                parsed = json.loads(args_json) if args_json else {}
            except json.JSONDecodeError as e:
                # Lowercase the error message for consistent matching
                error_msg = str(e).lower()
                return ToolCallOutcome(
                    result=CallToolResult(
                        content=[
                            mcp_types.TextContent(type="text", text=f"Invalid JSON in tool arguments: {error_msg}")
                        ],
                        structured_content=None,
                        meta=None,
                        is_error=True,
                    )
                )
            if not isinstance(parsed, dict):
                return ToolCallOutcome(
                    result=CallToolResult(
                        content=[
                            mcp_types.TextContent(
                                type="text", text=f"Tool arguments must be a JSON object, got {type(parsed).__name__}"
                            )
                        ],
                        structured_content=None,
                        meta=None,
                        is_error=True,
                    )
                )
            args: dict[str, Any] = parsed
            fastmcp_result = await self._mcp_client.call_tool(function_call.name, args, raise_on_error=False)
            return ToolCallOutcome(result=fastmcp_result)

        if self._parallel_tool_calls:
            await self._run_tool_calls_parallel(calls, function_calls, _invoke)
        else:
            await self._run_tool_calls_sequential(calls, function_calls, _invoke)
        self.pending_function_calls.clear()

    @staticmethod
    def _tool_error_to_outcome(e: ToolError) -> ToolCallOutcome:
        """Convert ToolError to an error tool result outcome (FastMCP CallToolResult)."""
        error_msg = str(e) if str(e) else "Tool error"
        return ToolCallOutcome(
            result=CallToolResult(
                content=[mcp_types.TextContent(type="text", text=f"Tool call failed: {error_msg}")],
                structured_content=None,
                meta=None,
                is_error=True,
            )
        )

    async def _run_tool_calls_parallel(
        self, calls: list[tuple[FunctionCallItem, str | None]], function_calls: list[FunctionCallItem], invoker
    ) -> None:
        results: dict[str, ToolCallOutcome] = {}
        abort_triggered = False

        async with anyio.create_task_group() as tg:
            cancelled_exc = anyio.get_cancelled_exc_class()

            async def runner(fc: FunctionCallItem, aj: str | None) -> None:
                nonlocal abort_triggered
                call_id = _require_call_id(fc)
                try:
                    outcome = await invoker(fc, aj)
                except cancelled_exc:
                    return
                except ToolError as e:
                    outcome = self._tool_error_to_outcome(e)

                results[call_id] = outcome
                if outcome.was_aborted:
                    abort_triggered = True
                    tg.cancel_scope.cancel()

            for function_call, args_json in calls:
                tg.start_soon(runner, function_call, args_json)

        had_error = False
        for function_call in function_calls:
            call_id = _require_call_id(function_call)
            outcome = results.get(call_id)
            if outcome is None:
                if not abort_triggered:
                    raise RuntimeError(f"Missing tool output for call_id={call_id!r}")
                outcome = ToolCallOutcome(result=_abort_result(), was_aborted=True)
            self._emit_tool_result(function_call, outcome.result)
            if outcome.was_aborted:
                had_error = True
        if had_error:
            self.finished = True

    async def _run_tool_calls_sequential(
        self, calls: list[tuple[FunctionCallItem, str | None]], function_calls: list[FunctionCallItem], invoker
    ) -> None:
        for i, (function_call, args_json) in enumerate(calls):
            try:
                outcome = await invoker(function_call, args_json)
            except ToolError as e:
                outcome = self._tool_error_to_outcome(e)

            self._emit_tool_result(function_call, outcome.result)
            if outcome.was_aborted:
                for remaining in function_calls[i + 1 :]:
                    self._emit_tool_result(remaining, _abort_result())
                self.finished = True
                break

    def to_openai_messages(self) -> list[InputItem]:
        """Convert transcript to typed OpenAI Responses input items.

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
        items: list[InputItem] = []
        for item in self._transcript:
            if isinstance(item, UserMessage | AssistantMessage | SystemMessage):
                # Use serialize/deserialize instead of model_copy(deep=True) to avoid pytest-xdist deadlock.
                # Root cause unclear: no circular refs in data, deepcopy works in isolation, but under
                # parallel test execution (pytest-xdist -n16) it hangs in Pydantic's __deepcopy__.
                # Hypothesis: pytest-xdist fork + Pydantic internal state (validators, cached schemas)
                # causes deepcopy memo dict corruption. Serialize/deserialize forces clean state.
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
                items.append(
                    FunctionCallOutputItem(call_id=item.call_id, output=_call_tool_result_to_openai(item.result))
                )
                continue
            raise TypeError(f"Unsupported transcript item for OpenAI input: {type(item)}")
        return items

    async def _run_one_phase(self):
        # Poll handlers sequentially - first non-NoAction decision wins
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

            # Notify handlers of compaction result
            for h in self._handlers:
                h.on_compaction_complete(compacted=result.compacted)

            return  # Continue to next iteration after compaction

        # Handle InjectItems: append all items to transcript and notify handlers
        if isinstance(decision, InjectItems):
            for item in decision.items:
                self._transcript.append(item)
                self._notify_handlers_for_transcript_item(item)

                # Handle item-specific side effects
                if isinstance(item, FunctionCallItem):
                    self.pending_function_calls.append(item)

            # Skip sampling this iteration
            # Main loop will execute any pending function calls, then iterate again
            return

        # Unify resp_output element type across branches for mypy
        resp_output: list[ReasoningItem | FunctionCallItem | FunctionCallOutputItem | AssistantMessageOut] | None = None

        # Determine whether to sample LLM
        should_sample_llm = False

        if isinstance(decision, NoAction):
            should_sample_llm = True
        else:
            raise TypeError(f"Unsupported loop decision: {type(decision).__name__}")

        if should_sample_llm:
            tool_choice = _tool_choice_from_policy(self._tool_policy)
            reasoning_param = build_reasoning_params(self._reasoning_effort, self._reasoning_summary)
            # Build OpenAI Responses tools list from MCP client
            mcp_tools = await self._mcp_client.list_tools()

            req = ResponsesRequest(
                input=self.to_openai_messages(),
                instructions=await self._build_effective_instructions(),
                stream=False,
                tool_choice=tool_choice,
                store=True,
                parallel_tool_calls=self._parallel_tool_calls,
                tools=[_make_strict_function_tool(t.name, t.description or "", t.inputSchema) for t in mcp_tools],
                reasoning=reasoning_param,
            )

            # Emit ApiRequest event for persistence
            # TODO: This stores O(N^2) data - full transcript state at each API call.
            # Consider storing only non-derivable data (e.g., instructions, tool schemas at each phase)
            # and reconstructing full request from transcript + metadata.
            request_id = uuid4()
            phase_number = sum(1 for evt in self._transcript if isinstance(evt, Response))
            for h in self._handlers:
                h.on_api_request_event(
                    ApiRequest(request=req, model=self._client.model, request_id=request_id, phase_number=phase_number)
                )

            resp = await self._client.responses_create(req)
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
            for h in self._handlers:
                h.on_response(
                    Response(response_id=resp.id, request_id=request_id, usage=usage, model=self._client.model)
                )
            resp_output = resp.output

        if resp_output is not None:
            self._process_resp_output(resp_output)
        # Note: Loop termination is now controlled by handlers (e.g., AbortIf, MaxTurnsHandler)
        # or explicit tool policies (e.g., RequireAnyTool prevents text-only responses)

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
        handled_call_ids = {evt.call_id for evt in self._transcript if isinstance(evt, ToolCallOutput)}
        for item in resp_output:
            # If this item has an id and we've already recorded it, skip
            iid = item.id if isinstance(item, ReasoningItem | FunctionCallItem) else None
            if isinstance(iid, str) and iid in existing_ids:
                continue
            if isinstance(item, ReasoningItem):
                self._transcript.append(item)
                self._notify_handlers_for_transcript_item(item)

            elif isinstance(item, AssistantMessageOut):
                # Convert API output type to transcript type
                self.assistant_text_chunks.append(item.text)
                assistant_msg = item.to_input_item()
                self._transcript.append(assistant_msg)
                self._notify_handlers_for_transcript_item(assistant_msg)

            elif isinstance(item, FunctionCallOutputItem):
                # Convert API output type to transcript type
                if item.output is None:
                    raise ValueError("FunctionCallOutputItem.output is None")
                result = _openai_to_mcp_result(item.output)
                original_call_id = item.call_id
                assert isinstance(original_call_id, str)
                assert original_call_id
                tool_output = ToolCallOutput(call_id=original_call_id, result=result)
                handled_call_ids.add(original_call_id)
                self._transcript.append(tool_output)
                self._notify_handlers_for_transcript_item(tool_output)
                # Update pending function calls
                if self.pending_function_calls:
                    self.pending_function_calls = [
                        fc for fc in self.pending_function_calls if fc.call_id != original_call_id
                    ]

            elif isinstance(item, FunctionCallItem):
                # Enforce a proper call_id for indexing/pending management
                call_id = _require_call_id(item)
                self._transcript.append(item)
                self._notify_handlers_for_transcript_item(item)
                # Store in map for quick lookup when processing outputs
                self._function_call_map[call_id] = item
                if call_id in handled_call_ids:
                    continue
                self.pending_function_calls.append(item)
            else:
                # Crash fast on unknown items to surface mismatches early
                raise TypeError(f"Unsupported Responses output item: {type(item)}")

    @classmethod
    async def create(
        cls,
        *,
        mcp_client: Client,
        handlers: Iterable[BaseHandler],
        client: OpenAIModelProto,
        reasoning_effort: ReasoningEffort | None = None,
        reasoning_summary: ReasoningSummary | None = None,
        parallel_tool_calls: bool = True,
        tool_policy: ToolPolicy,
        dynamic_instructions: Callable[[], Awaitable[str]] | None = None,
    ) -> Agent:
        """Create an Agent.

        Tool policy is set once at initialization and remains fixed throughout the agent's lifetime.
        Common values: RequireAnyTool() (typical), AllowAnyToolOrTextMessage(), ForbidAllTools().

        For static system prompts, use process_message(SystemMessage.text("...")) before calling run().
        For dynamic instructions that can change between phases, provide dynamic_instructions callback.
        """
        return cls(
            mcp_client=mcp_client,
            client=client,
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
            parallel_tool_calls=parallel_tool_calls,
            tool_policy=tool_policy,
            handlers=list(handlers),
            dynamic_instructions=dynamic_instructions,
        )

    def _emit_tool_result(self, function_call: FunctionCallItem, result: CallToolResult) -> None:
        """Emit a ToolCallOutput event and notify handlers.

        Converts FastMCP CallToolResult (dataclass) to mcp.types.CallToolResult (Pydantic) for storage.
        Sanitizes null bytes from tool results before emitting (PostgreSQL JSONB doesn't support them).
        """
        call_id = _require_call_id(function_call)
        # Convert FastMCP dataclass → MCP Pydantic for storage/persistence
        # TODO: Consider moving this conversion to persistence layer if events should store FastMCP types
        mcp_result = _fastmcp_to_mcp_result(result)
        # Sanitize null bytes and add warning if present
        sanitized = _sanitize_mcp_result(mcp_result)
        event = ToolCallOutput(call_id=call_id, result=sanitized)
        self._transcript.append(event)
        for h in self._handlers:
            h.on_tool_result_event(event)

    # Exposed for abort flows: synthesize aborted outputs for all pending calls
    def abort_pending_tool_calls(self) -> None:
        for fc in list(self.pending_function_calls):
            self._emit_tool_result(fc, _abort_result())
        self.pending_function_calls.clear()
