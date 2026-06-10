from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from typing import Any

import litellm
from litellm.types.utils import Choices

from tana.litellm_proxy.provider import register_litellm_provider

DEMO_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_demo_fact",
        "description": "Look up one short demo fact by topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The exact topic to look up.",
                }
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Call Tana's llmProxy through LiteLLM.")
    parser.add_argument("--model", default="tana/claude-3-5-sonnet-latest")
    parser.add_argument("--prompt", default="Reply with exactly: tana-litellm-ok")
    parser.add_argument("--system", default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--tool-demo", action="store_true", help="Ask the model to emit a demo function tool call.")
    parser.add_argument("--stream", action="store_true", help="Use LiteLLM streaming mode.")
    args = parser.parse_args()

    register_litellm_provider()
    messages: list[dict[str, str]] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    prompt = args.prompt
    tools = None
    if args.tool_demo:
        tools = [DEMO_TOOL]
        prompt = (
            "Call the lookup_demo_fact tool exactly once with topic "
            "'tana-litellm-tool'. Do not answer directly."
        )
    messages.append({"role": "user", "content": prompt})

    completion_kwargs = {
        "model": args.model,
        "messages": messages,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "tools": tools,
    }
    if args.stream:
        stream = litellm.completion(**completion_kwargs, stream=True)
        streamed_tool_calls: dict[str, dict[str, Any]] = {}
        printed_text = False
        for chunk in stream:
            choice = chunk.choices[0]
            delta = choice.delta
            content = _field(delta, "content")
            if content:
                print(content, end="", flush=True)
                printed_text = True
            tool_calls = _field(delta, "tool_calls")
            if tool_calls:
                for tool_call in tool_calls:
                    _merge_tool_call_delta(streamed_tool_calls, _tool_call_to_dict(tool_call))
        if streamed_tool_calls:
            if printed_text:
                print()
            print(json.dumps(list(streamed_tool_calls.values()), indent=2))
        else:
            print()
        return 0

    response = litellm.completion(**completion_kwargs)
    choice = response.choices[0]
    if not isinstance(choice, Choices):
        raise TypeError(f"unexpected streaming choice in non-streaming response: {type(choice).__name__}")
    if choice.message.tool_calls:
        print(
            json.dumps(
                [
                    {
                        "id": tool_call.id,
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                    for tool_call in choice.message.tool_calls
                ],
                indent=2,
            )
        )
    else:
        print(choice.message.content)
    return 0


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _tool_call_to_dict(tool_call: Any) -> dict[str, Any]:
    function = _field(tool_call, "function")
    return {
        "id": _field(tool_call, "id"),
        "type": _field(tool_call, "type"),
        "function": {
            "name": _field(function, "name"),
            "arguments": _field(function, "arguments"),
        },
    }


def _merge_tool_call_delta(tool_calls: dict[str, dict[str, Any]], delta: dict[str, Any]) -> None:
    key = str(delta.get("id") or len(tool_calls))
    existing = tool_calls.setdefault(
        key,
        {
            "id": delta.get("id"),
            "type": delta.get("type") or "function",
            "function": {"name": None, "arguments": ""},
        },
    )
    function = delta.get("function") or {}
    if delta.get("id"):
        existing["id"] = delta["id"]
    if delta.get("type"):
        existing["type"] = delta["type"]
    if function.get("name"):
        existing["function"]["name"] = function["name"]
    arguments = function.get("arguments")
    if arguments:
        existing_arguments = existing["function"]["arguments"]
        if arguments == "{}" and not existing_arguments:
            return
        if existing_arguments == "{}" and arguments.startswith("{"):
            existing["function"]["arguments"] = ""
        existing["function"]["arguments"] += arguments


if __name__ == "__main__":
    raise SystemExit(main())
