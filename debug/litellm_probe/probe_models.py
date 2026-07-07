from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict

from util.bazel.runfiles import get_required_path

DEFAULT_BASE_URL = "https://litellm.allegedly.works"
DEFAULT_CONFIG_RUNFILE = "ducktape/cluster/k8s/litellm/app/proxy-config.yaml"
REPORT_TEMPLATE_RUNFILE = "ducktape/debug/litellm_probe/report.html.j2"
RESULTS_JSONL = "results.jsonl"
EXPECTED_TEXT = "OK"
EXPECTED_TOOL_NAME = "lookup_demo_fact"
EXPECTED_TOOL_ARGS = {"topic": "litellm-probe"}
TEXT_PROMPT = f"Reply with exactly: {EXPECTED_TEXT}"
TOOL_PROMPT = f"Use the tool to look up the topic {EXPECTED_TOOL_ARGS['topic']}."
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"topic": {"type": "string"}},
    "required": ["topic"],
    "additionalProperties": False,
}
OPENAI_CHAT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": EXPECTED_TOOL_NAME,
        "description": "Look up a demo fact by topic.",
        "parameters": TOOL_PARAMETERS,
    },
}
FUNCTION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": EXPECTED_TOOL_NAME,
    "description": "Look up a demo fact by topic.",
    "parameters": TOOL_PARAMETERS,
}
ANTHROPIC_TOOL_SCHEMA: dict[str, Any] = {
    "name": EXPECTED_TOOL_NAME,
    "description": "Look up a demo fact by topic.",
    "input_schema": TOOL_PARAMETERS,
}
OPENAI_CHAT_TOOL_CHOICE: dict[str, Any] = {"type": "function", "function": {"name": EXPECTED_TOOL_NAME}}
FUNCTION_TOOL_CHOICE: dict[str, Any] = {"type": "function", "name": EXPECTED_TOOL_NAME}
ANTHROPIC_TOOL_CHOICE: dict[str, Any] = {"type": "tool", "name": EXPECTED_TOOL_NAME}
DEFAULT_BACKENDS = {"ollama"}
DEFAULT_PARITY_MODELS = {"gemini-2.5-flash", "glm-4.5-air-anthropic"}
TEXT_MODES = {"chat", "responses"}


class ProbeError(Exception):
    pass


class ModelProbe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    mode: str
    backend: str


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelProbe
    shape: str
    scenario: str
    status: str
    elapsed_seconds: float
    detail: str
    request_path: Path | None
    response_path: Path | None
    request_key: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Any


TextExtractor = Callable[[Any], list[str]]
ToolCallExtractor = Callable[[Any], list[ToolCall]]
DetailExtractor = Callable[[Any], str | None]


@dataclass(frozen=True)
class ProbeShape:
    name: str
    path: str
    input_field: str
    token_field: str
    extract_text: TextExtractor
    extract_tool_calls: ToolCallExtractor
    tool_schema: dict[str, Any] | None = None
    tool_choice: dict[str, Any] | None = None
    stream: bool = False
    temperature: float | None = None

    def build_request(self, model: ModelProbe, max_output_tokens: int, scenario: str) -> dict[str, Any]:
        prompt = TOOL_PROMPT if scenario == "tool" else TEXT_PROMPT
        body: dict[str, Any] = {"model": model.name, self.token_field: max_output_tokens}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.input_field == "messages":
            body["messages"] = [{"role": "user", "content": prompt}]
        else:
            body[self.input_field] = prompt
        if self.path == "/v1/responses":
            body["stream"] = self.stream
        if scenario == "tool":
            if self.tool_schema is None or self.tool_choice is None:
                raise ValueError(f"{self.name} does not define tool schema fields")
            body["tools"] = [deepcopy(self.tool_schema)]
            body["tool_choice"] = deepcopy(self.tool_choice)
        return body


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe configured LiteLLM models with tiny requests.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="LiteLLM proxy base URL.")
    parser.add_argument(
        "--api-key", default=None, help="LiteLLM API key. Prefer LITELLM_API_KEY to avoid shell history."
    )
    parser.add_argument(
        "--api-key-env", default="LITELLM_API_KEY", help="Environment variable containing the LiteLLM API key."
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="LiteLLM proxy config YAML. Defaults to the Bazel runfile."
    )
    parser.add_argument(
        "--backend",
        action="append",
        default=None,
        help=(
            "Probe models backed by this provider. Repeatable. Defaults to ollama plus cheap z.ai/gemini parity "
            "models. Use 'all' to disable filtering."
        ),
    )
    parser.add_argument(
        "--model", action="append", help="Probe one model. Repeatable. Defaults to all selected models."
    )
    parser.add_argument(
        "--shape",
        action="append",
        choices=sorted(SHAPES),
        default=None,
        help="API shape to probe. Repeatable. Defaults to all supported text shapes.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=["text", "tool"],
        default=None,
        help="Probe scenario. Repeatable. Defaults to text and tool.",
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=900.0, help="Per-request timeout, including cold model load."
    )
    parser.add_argument("--max-output-tokens", type=int, default=1024, help="Maximum generated tokens per probe.")
    parser.add_argument(
        "--response-dir",
        type=Path,
        default=None,
        help="Directory for request/response bodies. Defaults to /tmp/litellm-probe-responses/<timestamp>.",
    )
    parser.add_argument(
        "--html-report", type=Path, default=None, help="HTML report path. Defaults to <response-dir>/report.html."
    )
    parser.add_argument(
        "--aggregate-dir",
        type=Path,
        action="append",
        default=[],
        help="Prior probe response directory to include in the HTML report. Repeatable.",
    )
    parser.add_argument(
        "--resume-dir",
        type=Path,
        action="append",
        default=[],
        help="Prior probe response directory to resume from: keep OK records, rerun failed or missing cases.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Do not run probes; only render a report from --aggregate-dir result records.",
    )
    parser.add_argument("--continue-on-error", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--include-unsupported",
        action="store_true",
        help="Report unsupported non-text modes as failures instead of skips.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON lines instead of tab-separated text.")
    return parser.parse_args()


def _load_model_probes(config_path: Path | None) -> list[ModelProbe]:
    if config_path is None:
        config_path = get_required_path(DEFAULT_CONFIG_RUNFILE)
    config = yaml.safe_load(config_path.read_text())
    probes: list[ModelProbe] = []
    for entry in config["model_list"]:
        model_info = entry.get("model_info") or {}
        litellm_params = entry.get("litellm_params") or {}
        probes.append(
            ModelProbe(
                name=entry["model_name"], mode=model_info.get("mode", "chat"), backend=_backend_name(litellm_params)
            )
        )
    return probes


def _backend_name(litellm_params: dict[str, Any]) -> str:
    api_base = str(litellm_params.get("api_base", ""))
    if "ollama" in api_base:
        return "ollama"
    model = str(litellm_params.get("model", ""))
    if "api.z.ai" in api_base or "z.ai" in api_base:
        return "z.ai"
    if model.startswith("chatgpt/"):
        return "chatgpt"
    if "/" in model:
        return model.split("/", 1)[0]
    return "unknown"


def _default_selected_probe(probe: ModelProbe) -> bool:
    return probe.backend in DEFAULT_BACKENDS or probe.name in DEFAULT_PARITY_MODELS


def _headers(args: argparse.Namespace) -> dict[str, str]:
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except json.JSONDecodeError:
        return response.text


def _compact_detail(value: str) -> str:
    return value.strip().replace("\n", " ")[:240]


def _json_detail(value: Any) -> str:
    return json.dumps(value, sort_keys=True)[:240]


def _string_body_detail(body: Any) -> str | None:
    return _compact_detail(body) if isinstance(body, str) else None


def _error_detail(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    return _compact_detail(message) if isinstance(message, str) else None


def _chat_content_detail(body: Any) -> str | None:
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return _compact_detail(content)
    return None


def _json_field_detail(field: str) -> DetailExtractor:
    def extract(body: Any) -> str | None:
        if not isinstance(body, dict) or field not in body:
            return None
        return _json_detail(body[field])

    return extract


DETAIL_EXTRACTORS: tuple[DetailExtractor, ...] = (
    _string_body_detail,
    _error_detail,
    _chat_content_detail,
    _json_field_detail("output"),
    _json_field_detail("content"),
)


def _detail_from_body(body: Any) -> str:
    return next((detail for extract in DETAIL_EXTRACTORS if (detail := extract(body)) is not None), _json_detail(body))


def _save_body(path: Path | None, body: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _save_json_body(path: Path | None, body: dict[str, Any]) -> None:
    _save_body(path, json.dumps(body, indent=2, sort_keys=True))


def _post_json(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    request_path: Path | None,
    response_path: Path | None,
) -> tuple[Any, str]:
    _save_json_body(request_path, body)
    response = client.post(url, headers=headers, json=body)
    _save_body(response_path, response.text)
    parsed = _parse_json(response)
    detail = _detail_from_body(parsed)
    response.raise_for_status()
    return parsed, detail


def _first_chat_message(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return {}
    first = choices[0]
    if not isinstance(first, dict):
        return {}
    message = first.get("message")
    if isinstance(message, dict):
        return message
    return {}


def _chat_text(body: Any) -> list[str]:
    content = _first_chat_message(body).get("content")
    return [content] if isinstance(content, str) else []


def _parse_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _assert_tool_args(value: Any, body: Any, shape: str) -> None:
    args = _parse_json_object(value)
    if args == EXPECTED_TOOL_ARGS:
        return
    raise ProbeError(f"{shape} tool args are not {EXPECTED_TOOL_ARGS!r}: {_detail_from_body(body)}")


def _chat_tool_calls(body: Any) -> list[ToolCall]:
    tool_calls = _first_chat_message(body).get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    parsed: list[ToolCall] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str):
            parsed.append(ToolCall(name=name, arguments=function.get("arguments")))
    return parsed


def _responses_output_items(body: Any) -> list[Any]:
    if not isinstance(body, dict):
        return []
    output = body.get("output")
    if isinstance(output, list):
        return output
    return []


def _responses_text(body: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(body, dict):
        output_text = body.get("output_text")
        if isinstance(output_text, str):
            texts.append(output_text)
    for item in _responses_output_items(body):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    return texts


def _responses_tool_calls(body: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in _responses_output_items(body):
        if not isinstance(item, dict) or item.get("type") not in {"function_call", "tool_call"}:
            continue
        name = item.get("name")
        if isinstance(name, str):
            calls.append(ToolCall(name=name, arguments=item.get("arguments")))
    return calls


def _responses_body_from_stream_events(events: list[Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"output": []}
    output_items_by_index: dict[int, dict[str, Any]] = {}
    output_text_parts: list[str] = []
    function_call_arguments_by_index: dict[int, list[str]] = {}
    function_call_names_by_index: dict[int, str] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "response.completed":
            response = event.get("response")
            if isinstance(response, dict):
                return response
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                body["output"].append(item)
            continue
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                output_text_parts.append(delta)
            continue
        if event_type == "response.function_call_arguments.delta":
            index = event.get("output_index")
            delta = event.get("delta")
            if isinstance(index, int) and isinstance(delta, str):
                function_call_arguments_by_index.setdefault(index, []).append(delta)
            continue
        if event_type == "response.output_item.added":
            index = event.get("output_index")
            item = event.get("item")
            if isinstance(index, int) and isinstance(item, dict):
                output_items_by_index[index] = item
                name = item.get("name")
                if isinstance(name, str):
                    function_call_names_by_index[index] = name

    if output_text_parts:
        body["output"].append(
            {"type": "message", "content": [{"type": "output_text", "text": "".join(output_text_parts)}]}
        )
    for index, argument_parts in function_call_arguments_by_index.items():
        item = dict(output_items_by_index.get(index) or {})
        item["type"] = "function_call"
        item["name"] = function_call_names_by_index.get(index, item.get("name"))
        item["arguments"] = "".join(argument_parts)
        body["output"].append(item)
    return body


def _anthropic_text(body: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return texts


def _anthropic_tool_calls(body: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool_use":
                    continue
                name = part.get("name")
                if isinstance(name, str):
                    calls.append(ToolCall(name=name, arguments=part.get("input")))
    return calls


def _assert_expected_text(shape: ProbeShape, body: Any) -> str:
    if any(text.strip() == EXPECTED_TEXT for text in shape.extract_text(body)):
        return ""
    raise ProbeError(f"{shape.name} final text is not {EXPECTED_TEXT!r}: {_detail_from_body(body)}")


def _assert_expected_tool_call(shape: ProbeShape, body: Any) -> str:
    for call in shape.extract_tool_calls(body):
        if call.name != EXPECTED_TOOL_NAME:
            continue
        _assert_tool_args(call.arguments, body, shape.name)
        return ""
    raise ProbeError(f"no {shape.name} tool call named {EXPECTED_TOOL_NAME!r}: {_detail_from_body(body)}")


def _validate_for_scenario(shape: ProbeShape, scenario: str, body: Any) -> str:
    if scenario == "tool":
        return _assert_expected_tool_call(shape, body)
    return _assert_expected_text(shape, body)


SHAPE_DEFS = (
    ProbeShape(
        name="openai-chat",
        path="/v1/chat/completions",
        input_field="messages",
        token_field="max_tokens",
        extract_text=_chat_text,
        extract_tool_calls=_chat_tool_calls,
        tool_schema=OPENAI_CHAT_TOOL_SCHEMA,
        tool_choice=OPENAI_CHAT_TOOL_CHOICE,
        temperature=0,
    ),
    ProbeShape(
        name="openai-responses",
        path="/v1/responses",
        input_field="input",
        token_field="max_output_tokens",
        extract_text=_responses_text,
        extract_tool_calls=_responses_tool_calls,
        tool_schema=FUNCTION_TOOL_SCHEMA,
        tool_choice=FUNCTION_TOOL_CHOICE,
    ),
    ProbeShape(
        name="openai-responses-streaming",
        path="/v1/responses",
        input_field="input",
        token_field="max_output_tokens",
        extract_text=_responses_text,
        extract_tool_calls=_responses_tool_calls,
        tool_schema=FUNCTION_TOOL_SCHEMA,
        tool_choice=FUNCTION_TOOL_CHOICE,
        stream=True,
    ),
    ProbeShape(
        name="anthropic-messages",
        path="/v1/messages",
        input_field="messages",
        token_field="max_tokens",
        extract_text=_anthropic_text,
        extract_tool_calls=_anthropic_tool_calls,
        tool_schema=ANTHROPIC_TOOL_SCHEMA,
        tool_choice=ANTHROPIC_TOOL_CHOICE,
        temperature=0,
    ),
)
SHAPES = {shape.name: shape for shape in SHAPE_DEFS}
DEFAULT_SHAPES = tuple(SHAPES)


def _request_url(base_url: str, shape: str) -> str:
    return f"{base_url}{SHAPES[shape].path}"


def _planned_request_body(model: ModelProbe, shape: str, scenario: str, max_output_tokens: int) -> dict[str, Any]:
    return SHAPES[shape].build_request(model, max_output_tokens, scenario)


def _request_key(url: str, body: dict[str, Any]) -> str:
    canonical_body = json.dumps(body, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"POST\n{url}\n{canonical_body}".encode()).hexdigest()
    return f"POST:{url}:sha256:{digest}"


def _request_url_from_key(request_key: str) -> str | None:
    if not request_key.startswith("POST:"):
        return None
    head, separator, _ = request_key.rpartition(":sha256:")
    if not separator:
        return None
    return head.removeprefix("POST:")


def _saved_exchange_matches_key(result: ProbeResult) -> bool:
    if result.request_path is None or result.response_path is None:
        return False
    if not result.request_path.exists() or not result.response_path.exists():
        return False
    url = _request_url_from_key(result.request_key)
    if url is None:
        return False
    try:
        request_body = json.loads(result.request_path.read_text())
    except json.JSONDecodeError:
        return False
    if not isinstance(request_body, dict):
        return False
    return _request_key(url, request_body) == result.request_key


def _post_streaming_responses(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    request_path: Path | None,
    response_path: Path | None,
) -> dict[str, Any]:
    _save_json_body(request_path, body)
    raw_lines: list[str] = []
    events: list[Any] = []
    with client.stream("POST", url, headers=headers, json=body) as response:
        if response.status_code >= 400:
            response.read()
            _save_body(response_path, response.text)
            response.raise_for_status()
        for line in response.iter_lines():
            raw_lines.append(line)
            if not line:
                continue
            if line.startswith("data: "):
                payload = line.removeprefix("data: ").strip()
                if payload and payload != "[DONE]":
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError as exc:
                        raise ProbeError(f"invalid Responses stream JSON: {payload}") from exc
        _save_body(response_path, "\n".join(raw_lines))
    return _responses_body_from_stream_events(events)


def _post_shape_request(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    model: ModelProbe,
    shape: ProbeShape,
    body: dict[str, Any],
    request_path: Path | None,
    response_path: Path | None,
) -> Any:
    url = _request_url(base_url, shape.name)
    if shape.stream:
        return _post_streaming_responses(client, url, headers, body, request_path, response_path)
    body_json, _ = _post_json(client, url, headers, body, request_path, response_path)
    return body_json


def _probe_one(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    model: ModelProbe,
    shape: str,
    scenario: str,
    max_output_tokens: int,
    include_unsupported: bool,
    request_path: Path | None,
    response_path: Path | None,
) -> ProbeResult:
    started = time.monotonic()
    shape_spec = SHAPES[shape]
    request_body = _planned_request_body(model, shape, scenario, max_output_tokens)
    request_key = _request_key(_request_url(base_url, shape), request_body)
    try:
        if model.mode not in TEXT_MODES:
            detail = f"unsupported non-text mode for {shape}/{scenario} probe: {model.mode}"
            status = "fail" if include_unsupported else "skip"
        else:
            response_body = _post_shape_request(
                client, base_url, headers, model, shape_spec, request_body, request_path, response_path
            )
            detail = _validate_for_scenario(shape_spec, scenario, response_body)
            status = "ok"
    except (httpx.HTTPError, ProbeError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        status = "fail"
    return ProbeResult(
        model=model,
        shape=shape,
        scenario=scenario,
        status=status,
        elapsed_seconds=time.monotonic() - started,
        detail=detail,
        request_path=request_path,
        response_path=response_path,
        request_key=request_key,
    )


def _print_result(result: ProbeResult, json_lines: bool) -> None:
    if json_lines:
        print(_result_record_json(result), flush=True)
        return
    print(
        "\t".join(
            [
                result.status,
                f"{result.elapsed_seconds:.1f}s",
                result.model.backend,
                result.model.mode,
                result.shape,
                result.scenario,
                result.model.name,
            ]
            + ([result.detail] if result.detail else [])
            + ([f"body={result.response_path}"] if result.status == "fail" and result.response_path is not None else [])
        ),
        flush=True,
    )


def _default_response_dir() -> Path:
    return Path("/tmp/litellm-probe-responses") / time.strftime("%Y%m%dT%H%M%S")


def _safe_filename_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def _artifact_base(response_dir: Path, model: ModelProbe, shape: str, scenario: str) -> Path:
    filename = "__".join([_safe_filename_part(model.name), _safe_filename_part(shape), _safe_filename_part(scenario)])
    return response_dir / filename


def _artifact_paths(response_dir: Path, model: ModelProbe, shape: str, scenario: str) -> tuple[Path, Path]:
    base = _artifact_base(response_dir, model, shape, scenario)
    return Path(f"{base}.request.json"), Path(f"{base}.response.json")


def _results_path(response_dir: Path) -> Path:
    return response_dir / RESULTS_JSONL


def _result_record_json(result: ProbeResult) -> str:
    record = result.model_dump(mode="json")
    record["elapsed_seconds"] = round(result.elapsed_seconds, 3)
    return json.dumps(record, sort_keys=True)


def _append_result_record(response_dir: Path, result: ProbeResult) -> None:
    path = _results_path(response_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(_result_record_json(result))
        file.write("\n")


def _load_result_records(response_dir: Path) -> list[ProbeResult]:
    path = _results_path(response_dir)
    if not path.exists():
        return []
    results: list[ProbeResult] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            results.append(ProbeResult.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"invalid result record in {path}:{line_number}: {exc}") from exc
    return results


def _load_aggregate_results(response_dirs: list[Path]) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for response_dir in response_dirs:
        loaded = _load_result_records(response_dir)
        if not loaded:
            print(f"warning: no {RESULTS_JSONL} records found in {response_dir}", file=sys.stderr)
        results.extend(loaded)
    return results


def _result_key(result: ProbeResult) -> str:
    return result.request_key


def _case_key(base_url: str, model: ModelProbe, shape: str, scenario: str, max_output_tokens: int) -> str:
    body = _planned_request_body(model, shape, scenario, max_output_tokens)
    return _request_key(_request_url(base_url, shape), body)


def _merge_results(results: list[ProbeResult]) -> dict[str, ProbeResult]:
    merged: dict[str, ProbeResult] = {}
    for result in results:
        merged[_result_key(result)] = result
    return merged


def _planned_request_keys(
    base_url: str, probes: list[ModelProbe], shapes: list[str], scenarios: list[str], max_output_tokens: int
) -> set[str]:
    return {
        _case_key(base_url, probe, shape, scenario, max_output_tokens)
        for probe in probes
        for shape in shapes
        for scenario in scenarios
    }


def _pretty_file(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    text = path.read_text()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(parsed, indent=2, sort_keys=True)


def _status_counts(results: list[ProbeResult]) -> dict[str, int]:
    return dict(Counter(result.status for result in results))


def _write_html_report(path: Path, results: list[ProbeResult], response_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = _status_counts(results)
    rows: list[dict[str, str]] = []
    for result in results:
        title = " ".join(
            [result.status.upper(), f"{result.elapsed_seconds:.1f}s", result.model.name, result.shape, result.scenario]
        )
        rows.append(
            {
                "status": result.status,
                "title": title,
                "backend": result.model.backend,
                "mode": result.model.mode,
                "detail": result.detail,
                "request_path": str(result.request_path),
                "request_body": _pretty_file(result.request_path),
                "response_path": str(result.response_path),
                "response_body": _pretty_file(result.response_path),
            }
        )
    template_path = get_required_path(REPORT_TEMPLATE_RUNFILE)
    env = Environment(
        loader=FileSystemLoader(template_path.parent), autoescape=select_autoescape(enabled_extensions=("html", "j2"))
    )
    html_text = env.get_template(template_path.name).render(
        counts=json.dumps(counts, sort_keys=True), response_dir=str(response_dir), rows=rows
    )
    path.write_text(html_text)


def _run_probe_matrix(
    client: httpx.Client,
    args: argparse.Namespace,
    base_url: str,
    headers: dict[str, str],
    probes: list[ModelProbe],
    shapes: list[str],
    scenarios: list[str],
    response_dir: Path,
    results_by_key: dict[str, ProbeResult],
) -> None:
    copied_resume_keys: set[str] = set()
    for probe in probes:
        for shape in shapes:
            for scenario in scenarios:
                key = _case_key(base_url, probe, shape, scenario, args.max_output_tokens)
                previous = results_by_key.get(key)
                if (
                    args.resume_dir
                    and previous is not None
                    and previous.status == "ok"
                    and _saved_exchange_matches_key(previous)
                ):
                    if key not in copied_resume_keys:
                        _append_result_record(response_dir, previous)
                        copied_resume_keys.add(key)
                    continue
                request_path, response_path = _artifact_paths(response_dir, probe, shape, scenario)
                result = _probe_one(
                    client,
                    base_url,
                    headers,
                    probe,
                    shape,
                    scenario,
                    args.max_output_tokens,
                    args.include_unsupported,
                    request_path,
                    response_path,
                )
                results_by_key[key] = result
                _append_result_record(response_dir, result)
                _print_result(result, args.json)
                if result.status == "fail" and not args.continue_on_error:
                    return


def main() -> int:
    args = _parse_args()
    base_url = args.base_url.rstrip("/")
    all_probes = _load_model_probes(args.config)
    if args.model:
        selected = set(args.model)
        unknown = sorted(selected - {probe.name for probe in all_probes})
        if unknown:
            print(f"Unknown model(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        probes = [probe for probe in all_probes if probe.name in selected]
    elif args.backend:
        backends = set(args.backend)
        probes = all_probes if "all" in backends else [probe for probe in all_probes if probe.backend in backends]
    else:
        probes = [probe for probe in all_probes if _default_selected_probe(probe)]

    headers = _headers(args)
    shapes = args.shape or list(DEFAULT_SHAPES)
    scenarios = args.scenario or ["text", "tool"]
    planned_keys = _planned_request_keys(base_url, probes, shapes, scenarios, args.max_output_tokens)
    response_dir = args.response_dir or _default_response_dir()
    response_dir.mkdir(parents=True, exist_ok=True)
    print(f"response bodies: {response_dir}", file=sys.stderr, flush=True)
    report_path = args.html_report or response_dir / "report.html"
    prior_results = [
        result
        for result in _load_aggregate_results(args.aggregate_dir + args.resume_dir)
        if _result_key(result) in planned_keys
    ]
    results_by_key = _merge_results(prior_results)
    if args.report_only:
        results = list(results_by_key.values())
        _write_html_report(report_path, results, response_dir)
        print(f"html report: {report_path}", file=sys.stderr, flush=True)
        return 1 if any(result.status == "fail" for result in results) else 0
    timeout = httpx.Timeout(args.timeout_seconds, connect=15.0)
    interrupted = False
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            _run_probe_matrix(client, args, base_url, headers, probes, shapes, scenarios, response_dir, results_by_key)
    except KeyboardInterrupt:
        interrupted = True
        print("interrupted: writing report for completed probe records", file=sys.stderr, flush=True)
    results = list(results_by_key.values())
    _write_html_report(report_path, results, response_dir)
    print(f"html report: {report_path}", file=sys.stderr, flush=True)
    if interrupted:
        return 130
    return 1 if any(result.status == "fail" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
