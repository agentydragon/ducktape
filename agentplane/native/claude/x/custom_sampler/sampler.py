"""Experimental Messages loop. Provider blocks stay opaque across tool continuation."""

import argparse
import asyncio
import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any

import aiohttp

TOOL = {
    "name": "append_file",
    "description": "Append UTF-8 text to a file in the executor workspace.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
        "required": ["path", "text"],
        "additionalProperties": False,
    },
}


class ProviderError(RuntimeError):
    """A failed or incomplete provider stream; never automatically retried."""


async def receive_message(response: aiohttp.ClientResponse) -> dict[str, Any]:
    """Assemble supported Messages SSE deltas without projecting away opaque block fields.

    Unknown complete block types are retained. Unknown deltas fail closed because we cannot
    reconstruct them faithfully. Nothing in a partial response is eligible for tool execution.
    """
    response.raise_for_status()
    message: dict[str, Any] | None = None
    blocks: dict[int, dict[str, Any]] = {}
    inputs: dict[int, str] = {}
    open_blocks: set[int] = set()
    data: list[str] = []
    async for raw in response.content:
        line = raw.decode().rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        if line or not data:
            continue
        event = json.loads("\n".join(data))
        data.clear()
        match event["type"]:
            case "ping":
                pass
            case "error":
                raise ProviderError(f"provider stream error: {event['error']}")
            case "message_start":
                if message is not None:
                    raise ProviderError("duplicate message_start")
                message = event["message"]
            case "content_block_start":
                index = event["index"]
                if index in blocks:
                    raise ProviderError(f"duplicate block {index}")
                blocks[index] = event["content_block"]
                open_blocks.add(index)
            case "content_block_delta":
                index = event["index"]
                if index not in open_blocks:
                    raise ProviderError(f"delta for closed block {index}")
                delta = event["delta"]
                match delta["type"]:
                    case "input_json_delta":
                        inputs[index] = inputs.get(index, "") + delta["partial_json"]
                    case "text_delta" | "thinking_delta" | "signature_delta":
                        key = delta["type"].removesuffix("_delta")
                        blocks[index][key] = blocks[index].get(key, "") + delta[key]
                    case _:
                        raise ProviderError(f"unsupported delta: {delta['type']}")
            case "content_block_stop":
                index = event["index"]
                open_blocks.remove(index)
                if index in inputs:
                    blocks[index]["input"] = json.loads(inputs[index])
            case "message_delta":
                if message is None:
                    raise ProviderError("message_delta before message_start")
                message.update(event["delta"])
                message["usage"] = {**message.get("usage", {}), **event.get("usage", {})}
            case "message_stop":
                if message is None or open_blocks or sorted(blocks) != list(range(len(blocks))):
                    raise ProviderError("incomplete message")
                message["content"] = [blocks[index] for index in range(len(blocks))]
                return message
            case _:
                raise ProviderError(f"unsupported event: {event['type']}")
    raise ProviderError("stream ended before message_stop")


@dataclass
class Sampler:
    messages_url: str
    executor_url: str
    model: str
    credential: str
    # This is deliberately volatile. A crash after an effect but before its receipt is ambiguous.
    messages: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, prompt: str, *, max_requests: int) -> dict[str, Any]:
        if self.messages:
            raise ValueError("sampler instances are single-use; recovery requires an external effect audit")
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        self.messages.append({"role": "user", "content": prompt})
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), trust_env=True) as client:
            for _ in range(max_requests):
                async with client.post(
                    self.messages_url,
                    headers={"Authorization": f"Bearer {self.credential}", "anthropic-version": "2023-06-01"},
                    json={
                        "model": self.model,
                        "max_tokens": 2048,
                        "thinking": {"type": "enabled", "budget_tokens": 1024},
                        "stream": True,
                        "system": "Use append_file when asked to append a file. Report the tool result.",
                        "tools": [TOOL],
                        "messages": self.messages,
                    },
                ) as response:
                    message = await receive_message(response)
                content = message["content"]
                tool_uses = [block for block in content if block["type"] == "tool_use"]
                reason = message["stop_reason"]
                if reason != "tool_use" and tool_uses:
                    raise ProviderError(f"tool_use with {reason=}")
                if reason not in {"tool_use", "end_turn"} or (reason == "tool_use" and not tool_uses):
                    raise ProviderError(f"unsupported {reason=}")
                self.messages.append({"role": "assistant", "content": copy.deepcopy(content)})
                if reason == "end_turn":
                    return message
                results = []
                # No automatic retry, concurrency or replay of an executor operation.
                for tool in tool_uses:
                    if tool["name"] != TOOL["name"]:
                        raise ProviderError(f"unsupported tool: {tool['name']}")
                    async with client.post(self.executor_url, json=tool["input"]) as response:
                        response.raise_for_status()
                        result = await response.json()
                    results.append({"type": "tool_result", "tool_use_id": tool["id"], "content": json.dumps(result)})
                self.messages.append({"role": "user", "content": results})
        raise ProviderError(f"request budget exhausted: {max_requests}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages-url", required=True)
    parser.add_argument("--executor-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--credential-env", required=True)
    parser.add_argument("--max-requests", type=int, default=4)
    parser.add_argument("prompt")
    args = parser.parse_args()
    sampler = Sampler(args.messages_url, args.executor_url, args.model, os.environ[args.credential_env])
    print(json.dumps(asyncio.run(sampler.run(args.prompt, max_requests=args.max_requests))))


if __name__ == "__main__":
    main()
