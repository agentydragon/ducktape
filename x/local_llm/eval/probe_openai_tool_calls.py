"""Capture repeated OpenAI-compatible tool-call and tool-result probes."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def post_json(url: str, payload: dict[str, object]) -> tuple[dict[str, object], float]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        body = {"http_error": error.code, "body": error.read().decode(errors="replace")}
    return body, time.perf_counter() - start


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:11500/v1")
    parser.add_argument("--model", default="gemma4:e4b")
    parser.add_argument("--file", type=Path, required=True, help="fixture contents returned by the simulated read tool")
    parser.add_argument("--tool-path", default="main.py", help="short logical path requested from the model")
    parser.add_argument(
        "--expected-followup-substring",
        required=True,
        help="known identifier the assistant must report after receiving the fixture contents",
    )
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    file_path = args.file.resolve(strict=True)
    file_content = file_path.read_text()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a local file from the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"filePath": {"type": "string"}},
                    "required": ["filePath"],
                },
            },
        }
    ]
    endpoint = f"{args.base_url.rstrip('/')}/chat/completions"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    all_repeats_succeeded = args.repeats > 0
    with args.output.open("w", encoding="utf-8") as output:
        for repeat in range(args.repeats):
            started_at = datetime.now(UTC)
            payload: dict[str, object] = {
                "model": args.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a coding agent. Use the read tool when asked to inspect a local file.",
                    },
                    {
                        "role": "user",
                        "content": f"Use the read tool to inspect {args.tool_path}. Identify the first function name defined in the file and say only that name.",
                    },
                ],
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0,
                "top_p": 0.95,
                "max_tokens": args.max_tokens,
                "reasoning_effort": "none",
                "stream": False,
            }
            first_response, first_wall = post_json(endpoint, payload)
            choices_value = first_response.get("choices", [])
            choices = choices_value if isinstance(choices_value, list) else []
            first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            message_value = first_choice.get("message", {})
            message = message_value if isinstance(message_value, dict) else {}
            tool_calls_value = message.get("tool_calls", [])
            tool_calls = (
                [tool_call for tool_call in tool_calls_value if isinstance(tool_call, dict)]
                if isinstance(tool_calls_value, list)
                else []
            )
            row: dict[str, object] = {
                "repeat": repeat,
                "started_at_utc": started_at.isoformat(),
                "request": payload,
                "response": first_response,
                "wall_seconds": first_wall,
                "structured_tool_call": bool(tool_calls),
                "expected_tool_name": "read",
                "expected_tool_path": args.tool_path,
                "expected_followup_substring": args.expected_followup_substring,
                "first_finish_reason": first_choice.get("finish_reason"),
                "fixture_content_source": str(file_path),
                "successful_tool_roundtrip": False,
            }
            if tool_calls:
                first_messages = payload["messages"]
                assert isinstance(first_messages, list)
                followup_messages: list[object] = list(first_messages)
                followup_messages.append(message)
                requested_paths: list[str | None] = []
                requested_names: list[str | None] = []
                tool_results_match = True
                for tool_call in tool_calls:
                    function_value = tool_call.get("function", {})
                    function = function_value if isinstance(function_value, dict) else {}
                    requested_name = function.get("name")
                    requested_names.append(requested_name if isinstance(requested_name, str) else None)
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    requested_path_value = arguments.get("filePath") if isinstance(arguments, dict) else None
                    requested_path = requested_path_value if isinstance(requested_path_value, str) else None
                    requested_paths.append(requested_path)
                    tool_output = (
                        file_content
                        if requested_path == args.tool_path
                        else (f"file not found: {requested_path}" if requested_path else "invalid read arguments")
                    )
                    tool_results_match = tool_results_match and tool_output == file_content
                    followup_messages.append(
                        {"role": "tool", "tool_call_id": tool_call.get("id", "read-result"), "content": tool_output}
                    )
                followup_payload = {**payload, "messages": followup_messages}
                followup_response, followup_wall = post_json(endpoint, followup_payload)
                row["followup_request"] = followup_payload
                row["followup_response"] = followup_response
                row["followup_wall_seconds"] = followup_wall
                followup_choices = followup_response.get("choices", [])
                followup_choice = (
                    followup_choices[0]
                    if isinstance(followup_choices, list) and followup_choices and isinstance(followup_choices[0], dict)
                    else {}
                )
                followup_message_value = followup_choice.get("message", {})
                followup_message = followup_message_value if isinstance(followup_message_value, dict) else {}
                followup_content_value = followup_message.get("content", "")
                followup_content = followup_content_value if isinstance(followup_content_value, str) else ""
                row["followup_finish_reason"] = followup_choice.get("finish_reason")
                row["followup_assistant_content"] = followup_content
                row["followup_has_assistant_message"] = followup_message.get("role") == "assistant"
                names_match = bool(requested_names) and all(name == "read" for name in requested_names)
                paths_match = bool(requested_paths) and all(
                    requested_path == args.tool_path for requested_path in requested_paths
                )
                answer_matches = args.expected_followup_substring in followup_content
                row["tool_call_names"] = requested_names
                row["tool_call_names_match"] = names_match
                row["tool_call_paths"] = requested_paths
                row["tool_call_paths_match"] = paths_match
                row["tool_results_match_fixture"] = tool_results_match
                row["followup_answer_matches"] = answer_matches
                successful = bool(
                    row["followup_has_assistant_message"]
                    and names_match
                    and paths_match
                    and tool_results_match
                    and answer_matches
                )
                row["successful_tool_roundtrip"] = successful
            else:
                successful = False
            all_repeats_succeeded = all_repeats_succeeded and successful
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            print(
                json.dumps(
                    {
                        "repeat": repeat,
                        "structured_tool_call": bool(tool_calls),
                        "successful_tool_roundtrip": successful,
                        "wall_seconds": first_wall,
                    }
                )
            )
    return 0 if all_repeats_succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
