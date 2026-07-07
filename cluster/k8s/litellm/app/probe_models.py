from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from jinja2 import Environment

from util.bazel.runfiles import get_required_path

DEFAULT_BASE_URL = "https://litellm.allegedly.works"
DEFAULT_CONFIG_RUNFILE = "ducktape/cluster/k8s/litellm/app/proxy-config.yaml"
RESULTS_JSONL = "results.jsonl"
EXPECTED_TEXT = "OK"
EXPECTED_TOOL_NAME = "lookup_demo_fact"
EXPECTED_TOOL_ARGS = {"topic": "litellm-probe"}
DEFAULT_BACKENDS = {"ollama"}
DEFAULT_PARITY_MODELS = {"gemini-2.5-flash", "glm-4.5-air-anthropic"}
TEXT_MODES = {"chat", "responses"}


@dataclass(frozen=True)
class ModelProbe:
    name: str
    mode: str
    backend: str


@dataclass(frozen=True)
class ProbeResult:
    model: ModelProbe
    shape: str
    scenario: str
    status: str
    elapsed_seconds: float
    detail: str
    request_path: Path | None
    response_path: Path | None
    request_key: str | None = None


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
        choices=["openai-chat", "openai-responses", "anthropic-messages"],
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


def _detail_from_body(body: Any) -> str:
    if isinstance(body, str):
        return body.strip().replace("\n", " ")[:240]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message.replace("\n", " ")[:240]
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content.strip().replace("\n", " ")[:240]
        output = body.get("output")
        if output is not None:
            return json.dumps(output)[:240]
        content = body.get("content")
        if content is not None:
            return json.dumps(content)[:240]
    return json.dumps(body, sort_keys=True)[:240]


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


def _assert_chat_text(body: Any) -> str:
    content = _first_chat_message(body).get("content")
    if isinstance(content, str) and content.strip() == EXPECTED_TEXT:
        return ""
    raise ValueError(f"chat final text is not {EXPECTED_TEXT!r}: {_detail_from_body(body)}")


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


def _assert_tool_args(value: Any, body: Any) -> None:
    args = _parse_json_object(value)
    if args == EXPECTED_TOOL_ARGS:
        return
    raise ValueError(f"tool args are not {EXPECTED_TOOL_ARGS!r}: {_detail_from_body(body)}")


def _assert_chat_tool_call(body: Any) -> str:
    tool_calls = _first_chat_message(body).get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        raise ValueError(f"no chat tool_calls in response: {_detail_from_body(body)}")
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        if function.get("name") != EXPECTED_TOOL_NAME:
            continue
        _assert_tool_args(function.get("arguments"), body)
        return ""
    raise ValueError(f"no chat tool_call named {EXPECTED_TOOL_NAME!r}: {_detail_from_body(body)}")


def _responses_output_items(body: Any) -> list[Any]:
    if not isinstance(body, dict):
        return []
    output = body.get("output")
    if isinstance(output, list):
        return output
    return []


def _assert_responses_text(body: Any) -> str:
    if isinstance(body, dict):
        output_text = body.get("output_text")
        if isinstance(output_text, str) and output_text.strip() == EXPECTED_TEXT:
            return ""
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
            if isinstance(text, str) and text.strip() == EXPECTED_TEXT:
                return ""
    raise ValueError(f"Responses final text is not {EXPECTED_TEXT!r}: {_detail_from_body(body)}")


def _assert_responses_tool_call(body: Any) -> str:
    for item in _responses_output_items(body):
        if not isinstance(item, dict) or item.get("type") not in {"function_call", "tool_call"}:
            continue
        if item.get("name") != EXPECTED_TOOL_NAME:
            continue
        _assert_tool_args(item.get("arguments"), body)
        return ""
    raise ValueError(f"no Responses function_call named {EXPECTED_TOOL_NAME!r}: {_detail_from_body(body)}")


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


def _assert_anthropic_text(body: Any) -> str:
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip() == EXPECTED_TEXT:
                    return ""
    raise ValueError(f"Anthropic final text is not {EXPECTED_TEXT!r}: {_detail_from_body(body)}")


def _assert_anthropic_tool_call(body: Any) -> str:
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool_use":
                    continue
                if part.get("name") == EXPECTED_TOOL_NAME:
                    _assert_tool_args(part.get("input"), body)
                    return ""
    raise ValueError(f"no Anthropic tool_use named {EXPECTED_TOOL_NAME!r}: {_detail_from_body(body)}")


def _tool_schema_openai() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": EXPECTED_TOOL_NAME,
            "description": "Look up a demo fact by topic.",
            "parameters": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
                "additionalProperties": False,
            },
        },
    }


def _tool_schema_responses() -> dict[str, Any]:
    return {
        "type": "function",
        "name": EXPECTED_TOOL_NAME,
        "description": "Look up a demo fact by topic.",
        "parameters": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
            "additionalProperties": False,
        },
    }


def _tool_schema_anthropic() -> dict[str, Any]:
    return {
        "name": EXPECTED_TOOL_NAME,
        "description": "Look up a demo fact by topic.",
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
            "additionalProperties": False,
        },
    }


def _chat_request_body(model: str, max_output_tokens: int, scenario: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": f"Reply with exactly: {EXPECTED_TEXT}"}],
        "temperature": 0,
        "max_tokens": max_output_tokens,
    }
    if scenario == "tool":
        body["messages"] = [
            {"role": "user", "content": f"Use the tool to look up the topic {EXPECTED_TOOL_ARGS['topic']}."}
        ]
        body["tools"] = [_tool_schema_openai()]
        body["tool_choice"] = {"type": "function", "function": {"name": EXPECTED_TOOL_NAME}}
    return body


def _responses_request_body(model: str, max_output_tokens: int, scenario: str, stream: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "input": f"Reply with exactly: {EXPECTED_TEXT}",
        "stream": stream,
        "max_output_tokens": max_output_tokens,
    }
    if scenario == "tool":
        body["input"] = f"Use the tool to look up the topic {EXPECTED_TOOL_ARGS['topic']}."
        body["tools"] = [_tool_schema_responses()]
        body["tool_choice"] = {"type": "function", "name": EXPECTED_TOOL_NAME}
    return body


def _anthropic_request_body(model: str, max_output_tokens: int, scenario: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_output_tokens,
        "temperature": 0,
        "messages": [{"role": "user", "content": f"Reply with exactly: {EXPECTED_TEXT}"}],
    }
    if scenario == "tool":
        body["messages"] = [
            {"role": "user", "content": f"Use the tool to look up the topic {EXPECTED_TOOL_ARGS['topic']}."}
        ]
        body["tools"] = [_tool_schema_anthropic()]
        body["tool_choice"] = {"type": "tool", "name": EXPECTED_TOOL_NAME}
    return body


def _request_url(base_url: str, shape: str) -> str:
    if shape == "openai-chat":
        return f"{base_url}/v1/chat/completions"
    if shape == "openai-responses":
        return f"{base_url}/v1/responses"
    if shape == "anthropic-messages":
        return f"{base_url}/v1/messages"
    raise ValueError(f"unsupported shape: {shape}")


def _planned_request_body(model: ModelProbe, shape: str, scenario: str, max_output_tokens: int) -> dict[str, Any]:
    if shape == "openai-chat":
        return _chat_request_body(model.name, max_output_tokens, scenario)
    if shape == "openai-responses":
        return _responses_request_body(model.name, max_output_tokens, scenario, stream=model.mode == "responses")
    if shape == "anthropic-messages":
        return _anthropic_request_body(model.name, max_output_tokens, scenario)
    raise ValueError(f"unsupported shape: {shape}")


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
    if result.request_key is None or result.request_path is None or result.response_path is None:
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


def _probe_chat(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    model: str,
    max_output_tokens: int,
    scenario: str,
    request_path: Path | None,
    response_path: Path | None,
) -> str:
    body = _chat_request_body(model, max_output_tokens, scenario)
    body_json, _ = _post_json(client, f"{base_url}/v1/chat/completions", headers, body, request_path, response_path)
    if scenario == "tool":
        return _assert_chat_tool_call(body_json)
    return _assert_chat_text(body_json)


def _probe_responses(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    model: str,
    max_output_tokens: int,
    scenario: str,
    stream: bool,
    request_path: Path | None,
    response_path: Path | None,
) -> str:
    body = _responses_request_body(model, max_output_tokens, scenario, stream)
    _save_json_body(request_path, body)
    if not stream:
        body_json, _ = _post_json(client, f"{base_url}/v1/responses", headers, body, None, response_path)
        if scenario == "tool":
            return _assert_responses_tool_call(body_json)
        return _assert_responses_text(body_json)
    raw_lines: list[str] = []
    events: list[Any] = []
    with client.stream("POST", f"{base_url}/v1/responses", headers=headers, json=body) as response:
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
                    with suppress(json.JSONDecodeError):
                        events.append(json.loads(payload))
        _save_body(response_path, "\n".join(raw_lines))
        body_json = _responses_body_from_stream_events(events)
        if scenario == "tool":
            return _assert_responses_tool_call(body_json)
        return _assert_responses_text(body_json)


def _probe_anthropic_messages(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    model: str,
    max_output_tokens: int,
    scenario: str,
    request_path: Path | None,
    response_path: Path | None,
) -> str:
    body = _anthropic_request_body(model, max_output_tokens, scenario)
    body_json, _ = _post_json(client, f"{base_url}/v1/messages", headers, body, request_path, response_path)
    if scenario == "tool":
        return _assert_anthropic_tool_call(body_json)
    return _assert_anthropic_text(body_json)


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
    request_key = _request_key(
        _request_url(base_url, shape), _planned_request_body(model, shape, scenario, max_output_tokens)
    )
    try:
        if model.mode not in TEXT_MODES:
            detail = f"unsupported non-text mode for {shape}/{scenario} probe: {model.mode}"
            status = "fail" if include_unsupported else "skip"
        elif shape == "openai-chat":
            detail = _probe_chat(
                client, base_url, headers, model.name, max_output_tokens, scenario, request_path, response_path
            )
            status = "ok"
        elif shape == "openai-responses":
            detail = _probe_responses(
                client,
                base_url,
                headers,
                model.name,
                max_output_tokens,
                scenario,
                stream=model.mode == "responses",
                request_path=request_path,
                response_path=response_path,
            )
            status = "ok"
        elif shape == "anthropic-messages":
            detail = _probe_anthropic_messages(
                client, base_url, headers, model.name, max_output_tokens, scenario, request_path, response_path
            )
            status = "ok"
        else:
            detail = f"unsupported mode for {shape}/{scenario} probe: {model.mode}"
            status = "fail" if include_unsupported else "skip"
    except Exception as exc:
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
        print(json.dumps(_result_record(result), sort_keys=True), flush=True)
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


def _result_record(result: ProbeResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "model": result.model.name,
        "mode": result.model.mode,
        "backend": result.model.backend,
        "shape": result.shape,
        "scenario": result.scenario,
        "detail": result.detail,
        "request_path": str(result.request_path) if result.request_path is not None else None,
        "response_path": str(result.response_path) if result.response_path is not None else None,
        "request_key": result.request_key,
    }


def _append_result_record(response_dir: Path, result: ProbeResult) -> None:
    path = _results_path(response_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(_result_record(result), sort_keys=True))
        file.write("\n")


def _result_from_record(record: dict[str, Any]) -> ProbeResult:
    model = ModelProbe(name=str(record["model"]), mode=str(record["mode"]), backend=str(record["backend"]))
    request_path = record.get("request_path")
    response_path = record.get("response_path")
    return ProbeResult(
        model=model,
        shape=str(record["shape"]),
        scenario=str(record["scenario"]),
        status=str(record["status"]),
        elapsed_seconds=float(record["elapsed_seconds"]),
        detail=str(record.get("detail") or ""),
        request_path=Path(request_path) if isinstance(request_path, str) else None,
        response_path=Path(response_path) if isinstance(response_path, str) else None,
        request_key=str(record["request_key"]) if isinstance(record.get("request_key"), str) else None,
    )


def _load_result_records(response_dir: Path) -> list[ProbeResult]:
    path = _results_path(response_dir)
    if not path.exists():
        return []
    results: list[ProbeResult] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("record is not a JSON object")
            results.append(_result_from_record(record))
        except Exception as exc:
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


def _legacy_result_key(result: ProbeResult) -> str:
    return "\x1f".join([result.model.name, result.shape, result.scenario])


def _result_key(result: ProbeResult) -> str:
    return result.request_key or f"legacy:{_legacy_result_key(result)}"


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
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


_REPORT_TEMPLATE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>LiteLLM Probe Report</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; }
    details { border: 1px solid #ccc; border-radius: 6px; margin: 0.75rem 0; padding: 0.75rem; }
    details.ok { border-color: #7ab77a; }
    details.fail { border-color: #d36b6b; }
    details.skip { border-color: #bbb; }
    summary { cursor: pointer; font-weight: 600; }
    pre { background: #f6f6f6; border-radius: 4px; overflow-x: auto; padding: 0.75rem; white-space: pre-wrap; }
  </style>
</head>
<body>
  <h1>LiteLLM Probe Report</h1>
  <p><strong>Response directory:</strong> {{ response_dir }}</p>
  <p><strong>Counts:</strong> {{ counts }}</p>
  {% for row in rows %}
  <details class="{{ row.status }}">
    <summary>{{ row.title }}</summary>
    <p><strong>Backend:</strong> {{ row.backend }} &nbsp; <strong>Mode:</strong> {{ row.mode }}</p>
    {% if row.detail %}<p><strong>Detail:</strong> {{ row.detail }}</p>{% endif %}
    <p><strong>Request:</strong> {{ row.request_path }}</p>
    <pre>{{ row.request_body }}</pre>
    <p><strong>Response:</strong> {{ row.response_path }}</p>
    <pre>{{ row.response_body }}</pre>
  </details>
  {% endfor %}
</body>
</html>
"""


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
    env = Environment(autoescape=True)
    html_text = env.from_string(_REPORT_TEMPLATE).render(
        counts=json.dumps(counts, sort_keys=True), response_dir=str(response_dir), rows=rows
    )
    path.write_text(html_text)


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
    shapes = args.shape or ["openai-chat", "openai-responses", "anthropic-messages"]
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
    stop_after_failure = False
    interrupted = False
    copied_resume_keys: set[str] = set()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for probe in probes:
                for shape in shapes:
                    for scenario in scenarios:
                        if stop_after_failure:
                            break
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
                            stop_after_failure = True
                            break
                    if stop_after_failure:
                        break
                if stop_after_failure:
                    break
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
