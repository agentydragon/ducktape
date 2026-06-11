import argparse
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
from collections import Counter
from contextlib import suppress
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, cast

import tiktoken
from jinja2 import Environment, FileSystemLoader, select_autoescape
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageCustomToolCall,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
    ChatCompletionUserMessageParam,
    CompletionCreateParams as ChatCompletionCreateParams,
)
from openai.types.responses import ResponseCreateParams, ResponseOutputMessage
from pydantic import BaseModel, TypeAdapter

from openai_utils.client_factory import get_async_openai
from openai_utils.model import (
    AssistantMessage,
    FunctionCallItem,
    InputItem,
    ResponsesResult,
    SystemMessage,
    UserMessage,
)
from openai_utils.retry import chat_create_with_retries, responses_create_with_retries
from sysrw.anthropic.types import Message as AnthropicMessage, MessageRole as AnthropicMessageRole
from sysrw.constants import TOOLS_HEADER
from sysrw.openai_typing import (
    MessageRole,
    chat_param_message_content_as_text,
    chat_param_message_role,
    chat_param_message_tool_calls,
    dump_chat_messages,
    parse_chat_messages,
    parse_response_messages,
    parse_tools_list,
    response_message_content_as_text,
    response_message_role,
)
from sysrw.prompts import build_grader_prompt
from sysrw.schemas import (
    CCRSample,
    ChatAssistantMessage,
    CrushSample,
    EvalGradeRecord,
    EvalSampleRecord,
    Grade,
    ResponsesAssistantMessage,
    Sample,
)
from sysrw.templates.loader import validate_template_file
from sysrw.translation import anthropic_messages_to_standard, anthropic_to_chat_messages

# Config
DEFAULT_DATASET_PATH = Path(__file__).parent / "data" / "dataset.jsonl"
DEFAULT_BASE = Path(__file__).parent / "runs"
MAX_INPUT_TOKENS = 272_000
MAX_TOTAL_TOKENS = 400_000
PER_OUTPUT_CAP = 128_000
SAFETY_TOKENS = 1_024
TARGET_PREFIX_TOKENS = 200_000  # budget for prefix JSON inside grader prompt


# Metrics helpers
class ConfidenceInterval(BaseModel):
    """95% confidence interval."""

    lcb: float
    ucb: float


class ToolingMetrics(BaseModel):
    """Computed metrics for tool usage."""

    total_samples: int
    text_only_pct: float
    with_tools_pct: float
    function_counts: Counter[str]
    function_pct: dict[str, float]


class SourceMetrics(BaseModel):
    """Metrics for a single source (ccr or crush)."""

    n: int
    mean: float
    ci95: ConfidenceInterval
    tooling: ToolingMetrics


class EvalCounters(BaseModel):
    """Evaluation process counters."""

    processed: int = 0
    skipped_input_tokens: int = 0
    sampler_errors: int = 0
    grader_errors: int = 0


class ModelConfig(BaseModel):
    """Model configuration for evaluation."""

    sampler: str
    evaluator: str


class SummaryMetrics(BaseModel):
    """Complete evaluation summary metrics."""

    n: int
    mean: float
    ci95: ConfidenceInterval
    counters: EvalCounters
    models: ModelConfig
    tooling: ToolingMetrics
    by_source: dict[str, SourceMetrics]


class ToolStats(BaseModel):
    total_samples: int = 0
    text_only: int = 0
    with_tools: int = 0
    function_counts: Counter[str] = Counter()

    def update_from_tool_calls(
        self, tool_calls: list[ChatCompletionMessageFunctionToolCall | ChatCompletionMessageCustomToolCall]
    ) -> None:
        """Update stats from a list of tool calls."""
        self.total_samples += 1
        if not tool_calls:
            self.text_only += 1
        else:
            self.with_tools += 1
            self.function_counts.update(
                tool_call.function.name
                if isinstance(tool_call, ChatCompletionMessageFunctionToolCall)
                else tool_call.custom.name
                for tool_call in tool_calls
            )

    def compute_metrics(self) -> ToolingMetrics:
        """Compute derived metrics from raw stats."""
        total_tool_calls = sum(self.function_counts.values())
        return ToolingMetrics(
            total_samples=self.total_samples,
            text_only_pct=(self.text_only / self.total_samples) if self.total_samples else 0.0,
            with_tools_pct=(self.with_tools / self.total_samples) if self.total_samples else 0.0,
            function_counts=self.function_counts,
            function_pct={
                k: (v / total_tool_calls) if total_tool_calls > 0 else 0.0 for k, v in self.function_counts.items()
            },
        )


# Models
SAMPLER_MODEL = "gpt-5"
GRADER_MODEL = "gpt-5"

# Paths
REWRITE_APPLY = resources.files("sysrw").joinpath("js/system_rewrite_apply.js")


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--template",
        required=True,
        help="Path to system prompt template file with mustache placeholders: {{toolsBlob}}, {{envGitBlobs}}, {{modelLine}}, {{mcpSection}}",
    )
    ap.add_argument(
        "--dataset",
        action="append",
        required=False,
        help=(
            "Dataset JSONL path; can be repeated to mix CCR and Crush samples in one run. "
            "Defaults to ./data/dataset.jsonl if omitted."
        ),
    )
    ap.add_argument(
        "--out-dir",
        required=False,
        help=(
            "Output directory. If provided, results are written directly here (no nesting). "
            "If omitted, writes to runs/<ts> or runs/baseline-<ts> (for current_effective_template.txt)."
        ),
    )
    ap.add_argument("--n", type=int, default=None, help="Limit number of samples to process")
    ap.add_argument("--concurrency", type=int, default=32, help="Number of samples to run in parallel")
    return ap.parse_args()


async def read_dataset(dataset_path: Path) -> list[Sample]:
    items: list[Sample] = []
    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            # Support both CCR (anthropic_request) and Crush (oai_request) entries
            if "anthropic_request" in rec:
                items.append(CCRSample.model_validate(rec))
                continue
            if "oai_request" in rec:
                items.append(CrushSample.model_validate(rec))
                continue
    return items


# --- OpenAI client ---


def estimate_tokens(text: str) -> int:
    enc = tiktoken.get_encoding("cl100k_base")
    # Encode special-token-looking sequences as plain text (no ValueError)
    return len(enc.encode(text, disallowed_special=()))


def tokens_for_chat_messages(msgs: Any) -> int:
    if (messages := parse_chat_messages(msgs)) is None:
        return 0
    parts: list[str] = []
    for message in messages:
        parts.append(chat_param_message_role(message))
        if text := chat_param_message_content_as_text(message):
            parts.append(text)
        if tool_calls := chat_param_message_tool_calls(message):
            for call in tool_calls:
                if args := call["function"]["arguments"]:
                    parts.append(args)
    return estimate_tokens("\n".join(parts))


def rewrite_system_with_template(system_text: str, template_path: Path) -> str:
    """Rewrite the system prompt via Node apply script.
    Fails clearly if Node.js is not available or the script errors out.
    """
    try:
        # Pass shared TOOLS_HEADER into the JS env to avoid magic strings
        env = {**os.environ, "TOOLS_HEADER": TOOLS_HEADER}
        proc = subprocess.run(
            ["node", str(REWRITE_APPLY), str(template_path)],
            input=system_text.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=60,
            env=env,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "Node.js ('node') not found in PATH; install Node or adjust PATH to use system rewrite"
        ) from e
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", errors="ignore"))
        raise RuntimeError(f"system rewrite failed with code {proc.returncode}")
    return proc.stdout.decode("utf-8")


ENV_INTRO = "Here is useful information about the environment you are running in:"
MODEL_PREFIX = "You are powered by the model"
MCP_HEADER = "# MCP Server Instructions"


def index_of_last_assistant_before_final(msgs: list[ChatCompletionMessageParam]) -> int | None:
    """Find index of last assistant message before the final message."""
    for i in range(len(msgs) - 2, -1, -1):
        if chat_param_message_role(msgs[i]) == MessageRole.ASSISTANT:
            return i
    return None


def index_of_last_assistant_in_anthropic_messages(messages: list[AnthropicMessage]) -> int | None:
    """Find index of last assistant message before the final message."""
    for i in range(len(messages) - 2, -1, -1):
        if messages[i].role == AnthropicMessageRole.ASSISTANT:
            return i
    return None


def convert_responses_tools_to_chat_functions(tools_val: Any) -> list[dict[str, Any]] | None:
    tools = parse_tools_list(tools_val)
    if not tools:
        return None
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        payload = tool.model_dump(mode="json", exclude_none=True) if isinstance(tool, BaseModel) else dict(tool)
        normalized.append(payload)
    return normalized


GRADE_TOOL = {
    "type": "function",
    "name": "grade",
    "description": "Return a 1-5 score and a short rationale.",
    "parameters": {
        "type": "object",
        "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 5}, "rationale": {"type": "string"}},
        "required": ["score", "rationale"],
        "additionalProperties": False,
    },
    "strict": True,
}


def parse_grade_from_responses(response: ResponsesResult) -> Grade:
    """Parse grade from ResponsesResult output.

    Extracts the 'grade' tool call from the response output and validates it as a Grade model.
    """
    if not response.output:
        raise RuntimeError("No output in responses")

    for item in response.output:
        if isinstance(item, FunctionCallItem) and item.name == "grade":
            if item.arguments is None:
                return cast(Grade, Grade.model_validate({}))
            return cast(Grade, Grade.model_validate_json(item.arguments))
    raise RuntimeError("No grade tool call in responses output")


def responses_prev_assistant_index(inp: Any) -> int | None:
    """Find index of previous assistant message in Responses API input."""
    parsed = parse_response_messages(inp)
    if parsed is None:
        return None
    for i in range(len(parsed) - 2, -1, -1):
        if response_message_role(parsed[i]) == MessageRole.ASSISTANT:
            return i
    return None


def responses_extract_system_text(inp: Any) -> str:
    """Extract and join all system message text from Responses API input."""
    parsed = parse_response_messages(inp)
    if parsed is None:
        return ""
    buf: list[str] = []
    for it in parsed:
        if response_message_role(it) != MessageRole.SYSTEM:
            continue
        buf.append(response_message_content_as_text(it))
    return "\n\n".join([t for t in buf if t])


def responses_slice_prefix(inp: Any, end_idx: int) -> list[InputItem]:
    """Slice Responses API input up to end_idx, excluding system messages."""
    out: list[InputItem] = []
    parsed = parse_response_messages(inp)
    if parsed is None:
        return out
    for it in parsed[:end_idx]:
        role = response_message_role(it)
        if role == MessageRole.USER:
            text = response_message_content_as_text(it)
            if text:
                out.append(UserMessage.text(text))
        elif role == MessageRole.ASSISTANT:
            text = response_message_content_as_text(it)
            if text:
                out.append(AssistantMessage.text(text))
    return out


def responses_to_ccr_messages(inp: list[ResponseOutputMessage]) -> list[ChatCompletionMessageParam]:
    """Convert Responses API messages to Chat Completion format (simplified text-only)."""
    msgs: list[ChatCompletionMessageParam] = []
    for it in inp:
        role = response_message_role(it)
        if role in (MessageRole.USER, MessageRole.ASSISTANT) and (txt := response_message_content_as_text(it).strip()):
            if role == MessageRole.USER:
                msgs.append(ChatCompletionUserMessageParam(role="user", content=txt))
            else:  # MessageRole.ASSISTANT
                msgs.append(ChatCompletionAssistantMessageParam(role="assistant", content=txt))
    return msgs


def generate_html_report(report_base: Path):
    """Generate HTML report from samples and grades JSONL files."""
    samples_path = report_base / "samples.jsonl"
    grades_path = report_base / "grades.jsonl"
    report_path = report_base / "report.html"
    # Build grades map
    grades_map: dict[str, Grade] = {}
    with grades_path.open("r", encoding="utf-8") as grades_file:
        for line in grades_file:
            grade_record = EvalGradeRecord.model_validate_json(line)
            correlation_id = grade_record.correlation_id
            if not correlation_id:
                continue
            parsed_grade = parse_grade_from_responses(grade_record.response)
            grades_map[correlation_id] = parsed_grade

    # Collect rows
    rows: list[dict[str, Any]] = []

    summary: dict[str, Any] = {}
    with (report_base / "summary.json").open("r", encoding="utf-8") as summary_file:
        summary = json.load(summary_file)

    template_file = report_base / "template.txt"

    with samples_path.open("r", encoding="utf-8") as samples_file:
        for line in samples_file:
            sample_json = json.loads(line)
            sample_record = EvalSampleRecord.model_validate(sample_json)
            correlation_id = sample_record.correlation_id or ""

            # Two display paths depending on source
            if isinstance(sample_record.new_assistant_message, ResponsesAssistantMessage):
                # Crush item: reconstruct minimal views from responses_input
                responses_input = sample_record.new_assistant_message.responses_input
                parsed_responses_input = parse_response_messages(responses_input)
                original_system = responses_extract_system_text(parsed_responses_input or responses_input)
                rewritten_system = rewrite_system_with_template(original_system or "", template_file)
                display_messages = responses_to_ccr_messages(parsed_responses_input) if parsed_responses_input else []
                last_assistant_index = index_of_last_assistant_before_final(display_messages)
                if last_assistant_index is None:
                    shared_prefix = display_messages
                    bad_branch = []
                else:
                    shared_prefix = [
                        msg
                        for msg in (display_messages[:last_assistant_index])
                        if chat_param_message_role(msg) != MessageRole.SYSTEM
                    ]
                    bad_branch = display_messages[last_assistant_index:]
            else:
                # CCR item - validate and use typed Anthropic structures
                if sample_record.anthropic_request is None:
                    continue
                system_field = sample_record.anthropic_request.system
                assert isinstance(system_field, str) or system_field is None, "system must be str or None"
                original_system = system_field or ""
                rewritten_system = rewrite_system_with_template(original_system, template_file)
                messages = anthropic_messages_to_standard(sample_record.anthropic_request.messages)
                last_assistant_index = index_of_last_assistant_before_final(messages)
                if last_assistant_index is None:
                    shared_prefix = messages
                    bad_branch = []
                else:
                    shared_prefix = [
                        msg
                        for msg in (messages[:last_assistant_index])
                        if chat_param_message_role(msg) != MessageRole.SYSTEM
                    ]
                    bad_branch = messages[last_assistant_index:]
            row_grade: Grade | None = grades_map.get(correlation_id)
            rows.append(
                {
                    "correlation_id": correlation_id,
                    "timestamp": sample_record.timestamp,
                    "original_system": original_system,
                    "rewritten_system": rewritten_system,
                    "shared_prefix": shared_prefix,
                    "bad_branch": bad_branch,
                    "alternative": sample_record.new_assistant_message.model_dump(mode="json"),
                    "grade": row_grade,
                }
            )

    # Jinja2 template
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html", "xml"])
    )
    template = env.get_template("report.html.j2")
    html_text = template.render(rows=rows, summary=summary)
    report_path.write_text(html_text, encoding="utf-8")


async def run_eval(
    template_path: Path,
    dataset_paths: list[Path],
    base_out: Path | None,
    n_limit: int | None = None,
    concurrency: int = 32,
    *,
    client: AsyncOpenAI,
):
    """Run eval pipeline. `client` (AsyncOpenAI) is required and must be injected by caller."""

    validate_template_file(template_path)
    # Determine output directory
    if base_out is not None:
        # Caller provided a final directory — use it directly (no nesting)
        out_dir = base_out
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = DEFAULT_BASE
        # Default layout: runs/<ts> for variants; runs/baseline-<ts> for baseline
        out_dir = base / f"baseline-{ts}" if template_path.name == "current_effective_template.txt" else base / f"{ts}"
    samples_out = out_dir / "samples.jsonl"
    grades_out = out_dir / "grades.jsonl"
    summary_out = out_dir / "summary.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    # copy template in
    with suppress(Exception):
        shutil.copyfile(template_path, out_dir / "template.txt")
    # Load dataset(s)
    # Load dataset(s) and concatenate
    dataset: list[Sample] = []
    for p in dataset_paths:
        dataset.extend(await read_dataset(p))
    total = len(dataset)
    if n_limit is not None:
        dataset = dataset[: max(0, int(n_limit))]
    selected = len(dataset)
    print(
        json.dumps(
            {
                "event": "startup",
                "dataset_paths": [str(p) for p in dataset_paths],
                "total": total,
                "selected": selected,
                "sampler_model": SAMPLER_MODEL,
                "grader_model": GRADER_MODEL,
            }
        )
    )

    progress_path = out_dir / "progress.jsonl"

    def log_event(event: dict[str, Any]):
        print(json.dumps(event))
        with progress_path.open("a", encoding="utf-8") as pg:
            pg.write(json.dumps(event) + "\n")

    counters = EvalCounters()

    # client is injected by caller (no implicit AsyncOpenAI() here)
    if client is None:
        raise ValueError("run_eval requires a non-None AsyncOpenAI client injected by caller")
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def process(item: Sample) -> tuple[EvalSampleRecord | None, EvalGradeRecord | None]:
        async with sem:
            log_event({"event": "process_start", "cid": item.correlation_id})
            # Branch by source without coercing persisted formats
            new_assistant_message: ChatAssistantMessage | ResponsesAssistantMessage
            messages_for_grader: list[ChatCompletionMessageParam]
            prev_assistant_index_for_grader: int
            sample_request: ChatCompletionCreateParams | ResponseCreateParams
            if isinstance(item, CCRSample):  # CCR
                # 1) Rewrite system via Node apply script
                anthropic_request = item.anthropic_request
                system_field = anthropic_request.system
                assert isinstance(system_field, str) or system_field is None, "system must be str or None"
                new_system = rewrite_system_with_template(system_field or "", template_path)
                # 2) Find last assistant message in Anthropic messages (before final user complaint)
                prev_assistant_index = index_of_last_assistant_in_anthropic_messages(anthropic_request.messages)
                if prev_assistant_index is None:
                    log_event({"correlation_id": item.correlation_id, "status": "no_prev_assistant"})
                    return None, None
                # 3) Build OpenAI sampling request BEFORE the bad assistant turn
                context_messages = anthropic_request.messages[:prev_assistant_index]
                openai_messages = anthropic_to_chat_messages(context_messages, new_system)
                input_tokens = tokens_for_chat_messages(openai_messages)
                log_event(
                    {
                        "event": "sampler_tokens",
                        "correlation_id": item.correlation_id,
                        "input_tokens": input_tokens,
                        "model": SAMPLER_MODEL,
                    }
                )
                if input_tokens > MAX_INPUT_TOKENS:
                    counters.skipped_input_tokens += 1
                    log_event(
                        {
                            "correlation_id": item.correlation_id,
                            "status": "skipped_input_too_large",
                            "input_tokens": input_tokens,
                        }
                    )
                    return None, None
                max_completion_tokens = max(1, min(PER_OUTPUT_CAP, MAX_TOTAL_TOKENS - input_tokens - SAFETY_TOKENS))
                tools_param = anthropic_request.tools
                chat_tools = convert_responses_tools_to_chat_functions(tools_param)
                sample_request = TypeAdapter(ChatCompletionCreateParams).validate_python(
                    {
                        "model": SAMPLER_MODEL,
                        "messages": dump_chat_messages(openai_messages),
                        "tools": chat_tools,
                        "tool_choice": "auto",
                        "parallel_tool_calls": True,
                        "max_completion_tokens": max_completion_tokens,
                    }
                )
                try:
                    sample = await chat_create_with_retries(client, sample_request)
                except Exception as e:
                    counters.sampler_errors += 1
                    msg = {"correlation_id": item.correlation_id, "status": "sampler_error", "error": str(e)}
                    log_event(msg)
                    return None, None
                new_assistant_message = ChatAssistantMessage(message=sample.choices[0].message)
                # For grader context: convert full Anthropic message list to ChatCompletionMessageParam
                messages_for_grader = anthropic_messages_to_standard(anthropic_request.messages)
                prev_assistant_index_for_grader = prev_assistant_index
            else:
                # Crush / Responses-native path
                payload = item.oai_request
                request_input = payload.get("input")
                # Extract original system and rewrite via Python fallback
                original_system = responses_extract_system_text(request_input)
                new_system = rewrite_system_with_template(original_system, template_path)
                # Find boundary and build context input (drop original system items)
                prev_assistant_index = responses_prev_assistant_index(request_input)
                if prev_assistant_index is None:
                    log_event({"correlation_id": item.correlation_id, "status": "no_prev_assistant"})
                    return None, None
                input_prefix = responses_slice_prefix(request_input, prev_assistant_index)
                # Prepend rewritten system entry
                responses_input_models: list[InputItem] = [SystemMessage.text(new_system), *input_prefix]
                responses_input_payload = [
                    item.model_dump(exclude_none=True) if isinstance(item, BaseModel) else item
                    for item in responses_input_models
                ]
                base_request: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
                base_request["input"] = responses_input_payload
                if not base_request.get("model"):
                    base_request["model"] = SAMPLER_MODEL
                sample_request = TypeAdapter(ResponseCreateParams).validate_python(base_request)
                try:
                    sample = await responses_create_with_retries(client, **sample_request)
                except Exception as e:
                    counters.sampler_errors += 1
                    msg = {"correlation_id": item.correlation_id, "status": "sampler_error", "error": str(e)}
                    log_event(msg)
                    return None, None
                new_assistant_message = ResponsesAssistantMessage(
                    responses_input=responses_input_models, responses_output=sample
                )
                # For grader context later, build ephemeral CCR-like messages
                parsed_request_input = parse_response_messages(request_input)
                if parsed_request_input is None:
                    log_event({"correlation_id": item.correlation_id, "status": "invalid_responses_input"})
                    return None, None
                messages_for_grader = responses_to_ccr_messages(parsed_request_input)
                prev_assistant_index_for_grader = index_of_last_assistant_before_final(messages_for_grader) or 0

            # 4) Build grading inputs
            messages = messages_for_grader
            base_prefix = messages[:-2] if len(messages) >= 2 else []
            base_prefix = [msg for msg in base_prefix if chat_param_message_role(msg) != MessageRole.SYSTEM]
            # Compute bad branch (inclusive of complaint)
            complaint_index = len(messages) - 1
            bad_branch = messages[prev_assistant_index_for_grader : complaint_index + 1]
            # Keep first 5 and last 5; truncate middle to fit token budget
            first_messages = base_prefix[:5]
            tail_messages = base_prefix[-5:] if len(base_prefix) > 5 else []
            middle_messages = base_prefix[5 : len(base_prefix) - len(tail_messages)] if len(base_prefix) > 10 else []

            # Build a provisional grader input to compute tokens; start from minimal
            prefix_messages = [*first_messages]  # start with first only
            grader_input = build_grader_prompt(prefix_messages + tail_messages, bad_branch, new_assistant_message)
            token_count = tokens_for_chat_messages(grader_input)
            # Greedily add middle messages until we hit budget
            added = 0
            for message in middle_messages:
                trial_grader_input = build_grader_prompt(
                    [*prefix_messages, message, *tail_messages], bad_branch, new_assistant_message
                )
                trial_token_count = tokens_for_chat_messages(trial_grader_input)
                if trial_token_count <= TARGET_PREFIX_TOKENS:
                    prefix_messages.append(message)
                    grader_input = trial_grader_input
                    token_count = trial_token_count
                    added += 1
                else:
                    break
            # Log truncation info
            log_event(
                {
                    "correlation_id": item.correlation_id,
                    "status": "grader_prefix_built",
                    "prefix_counts": {
                        "total": len(base_prefix),
                        "kept_first": len(first_messages),
                        "kept_last": len(tail_messages),
                        "added_middle": added,
                    },
                    "token_estimate": token_count,
                }
            )
            grader_messages = build_grader_prompt(prefix_messages, bad_branch, new_assistant_message)
            grader_request_messages = [
                SystemMessage.text(chat_param_message_content_as_text(grader_messages[0])).model_dump(),
                UserMessage.text(chat_param_message_content_as_text(grader_messages[1])).model_dump(),
            ]
            input_tokens_grader = tokens_for_chat_messages(grader_request_messages)
            if input_tokens_grader > MAX_INPUT_TOKENS:
                counters.skipped_input_tokens += 1
                log_event(
                    {
                        "correlation_id": item.correlation_id,
                        "status": "grader_skipped_input_too_large",
                        "input_tokens": input_tokens_grader,
                    }
                )
                return None, None
            max_output_tokens = max(1, min(PER_OUTPUT_CAP, MAX_TOTAL_TOKENS - input_tokens_grader - SAFETY_TOKENS))
            grade_request: ResponseCreateParams = TypeAdapter(ResponseCreateParams).validate_python(
                {
                    "model": GRADER_MODEL,
                    "input": grader_request_messages,
                    "tools": [GRADE_TOOL],
                    "tool_choice": {"type": "function", "name": "grade"},
                    "parallel_tool_calls": False,
                    "max_output_tokens": max_output_tokens,
                }
            )
            try:
                grade_response = await responses_create_with_retries(client, **grade_request)
            except Exception as e:
                counters.grader_errors += 1
                msg = {"correlation_id": item.correlation_id, "status": "grader_error", "error": str(e)}
                log_event(msg)
                return None, None
            # Validate grade parse
            try:
                _ = parse_grade_from_responses(grade_response)
            except Exception as e:
                counters.grader_errors += 1
                msg = {"correlation_id": item.correlation_id, "status": "grader_parse_error", "error": str(e)}
                log_event(msg)
                return None, None

            sample_record = EvalSampleRecord(
                request=sample_request,
                response=sample.model_dump(mode="json"),
                new_assistant_message=new_assistant_message,
                correlation_id=item.correlation_id,
                timestamp=item.timestamp,
                anthropic_request=item.anthropic_request if isinstance(item, CCRSample) else None,
            )
            grade_record = EvalGradeRecord(
                request=grade_request,
                response=grade_response,
                correlation_id=item.correlation_id,
                timestamp=item.timestamp,
            )
            return (sample_record, grade_record)

    # Build tasks and run aggregator loop (dedented from process)
    tasks = [process(item) for item in dataset]
    log_event({"event": "tasks_built", "count": len(tasks)})

    scores: list[float] = []
    # Secondary metrics: tooling usage
    tool_stats = ToolStats()
    # Per-source accumulators
    scores_by_source: dict[str, list[float]] = {"ccr": [], "crush": []}
    tool_stats_by_source: dict[str, ToolStats] = {"ccr": ToolStats(), "crush": ToolStats()}

    def compute_and_write_summary(_final: bool = False) -> dict[str, Any]:
        # Compute metrics for overall and per-source stats
        overall_metrics = tool_stats.compute_metrics()

        # CI helpers (normal approx, 95%)
        def _mk_basic(scores_list: list[float]) -> tuple[float, float, float, float]:
            if not scores_list:
                return 0.0, 0.0, 0.0, 0.0
            m = sum(scores_list) / len(scores_list)
            v = (sum((x - m) ** 2 for x in scores_list) / (len(scores_list) - 1)) if len(scores_list) > 1 else 0.0
            se_ = math.sqrt(v / len(scores_list)) if len(scores_list) > 0 else 0.0
            ci_ = 1.96 * se_
            return m, ci_, m - ci_, m + ci_

        # Compute mean and CI for overall scores
        mean, _ci95, lcb, ucb = _mk_basic(scores)

        by_source: dict[str, SourceMetrics] = {}
        for sname in ("ccr", "crush"):
            m_s, _ci_s, l_s, u_s = _mk_basic(scores_by_source[sname])
            source_metrics = tool_stats_by_source[sname].compute_metrics()
            by_source[sname] = SourceMetrics(
                n=len(scores_by_source[sname]),
                mean=m_s,
                ci95=ConfidenceInterval(lcb=l_s, ucb=u_s),
                tooling=source_metrics,
            )
        summary = SummaryMetrics(
            n=len(scores),
            mean=mean,
            ci95=ConfidenceInterval(lcb=lcb, ucb=ucb),
            counters=counters,
            models=ModelConfig(sampler=SAMPLER_MODEL, evaluator=GRADER_MODEL),
            tooling=overall_metrics,
            by_source=by_source,
        )
        with summary_out.open("w", encoding="utf-8") as f:
            json.dump(summary.model_dump(mode="json"), f, sort_keys=True)
        return summary.model_dump()

    with (
        samples_out.open("w", encoding="utf-8") as samples_output,
        grades_out.open("w", encoding="utf-8") as grades_output,
    ):
        log_event({"event": "as_completed_start", "count": len(tasks)})
        for fut in asyncio.as_completed(tasks):
            sample_record, grade_record = await fut
            # Determine source from sampling record shape
            source = None
            if sample_record:
                samples_output.write(json.dumps(sample_record.model_dump(mode="json"), sort_keys=True) + "\n")
                # Determine source from message type
                source = (
                    "crush" if isinstance(sample_record.new_assistant_message, ResponsesAssistantMessage) else "ccr"
                )
                # Extract tool calls from ChatAssistantMessage
                tool_calls = []
                if isinstance(sample_record.new_assistant_message, ChatAssistantMessage):
                    message_tool_calls = sample_record.new_assistant_message.message.tool_calls
                    tool_calls = list(message_tool_calls) if message_tool_calls is not None else []
                # Update tool usage stats
                tool_stats.update_from_tool_calls(tool_calls)
                # Per-source tool stats
                if source in tool_stats_by_source:
                    tool_stats_by_source[source].update_from_tool_calls(tool_calls)
            if grade_record:
                grades_output.write(json.dumps(grade_record.model_dump(mode="json"), sort_keys=True) + "\n")
                try:
                    grade = parse_grade_from_responses(grade_record.response)
                    score = float(grade.score)
                    scores.append(score)
                    if source in scores_by_source:
                        scores_by_source[source].append(score)
                    counters.processed += 1
                    summary_data = compute_and_write_summary(False)
                    print(
                        json.dumps(
                            {
                                "event": "grade_parsed",
                                "correlation_id": grade_record.correlation_id,
                                "score": score,
                                "source": source,
                                "n": summary_data["n"],
                                "mean": summary_data["mean"],
                                "ci95": summary_data["ci95"],
                                "models": summary_data["models"],
                            }
                        )
                    )
                except Exception as e:
                    counters.grader_errors += 1
                    log_event({"status": "aggregate_parse_error", "error": str(e)})

    # Final summary after all grades
    s_final = compute_and_write_summary(True)
    log_event(
        {
            "event": "summary_final",
            "n": s_final["n"],
            "mean": s_final["mean"],
            "ci95": s_final["ci95"],
            "models": s_final["models"],
        }
    )

    # Generate HTML report summarizing sequences per sample
    generate_html_report(out_dir)
    # Emit report path for convenience
    report_path = out_dir / "report.html"
    print(json.dumps({"event": "report_written", "path": str(report_path)}))
    print(str(report_path))


def main():
    args = parse_args()
    # Allow mixing multiple datasets in one run via repeated --dataset
    dataset_paths: list[Path] = [Path(p) for p in (args.dataset if args.dataset is not None else [])]
    if not dataset_paths:
        dataset_paths = [DEFAULT_DATASET_PATH]
    base_out = Path(args.out_dir) if args.out_dir else None
    asyncio.run(
        run_eval(Path(args.template), dataset_paths, base_out, args.n, args.concurrency, client=get_async_openai())
    )
