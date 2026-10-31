"""Manual CLIProxyAPI spike: request validity after interrupted reasoning streams.

Only synthetic function calls are generated. No model tool is executed.
"""

import copy
import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from util.testing.undeclared_outputs import undeclared_outputs_dir

PROMPT = (
    "Find the unique three-digit positive integer n satisfying n % 7 = 3, n % 11 = 5, "
    "and n % 13 = 7. Reason through the congruences carefully, then call report_answer "
    "exactly once with n. Do not use any other tool."
)
NOTICE = (
    "The preceding provider response was interrupted before its full response was committed. "
    "No tool call from that response was executed. Continue solving the original task."
)
SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}


@dataclass
class Sample:
    status: int
    terminal: str
    error: Any
    output: list[dict[str, Any]]
    events: list[dict[str, Any]]
    request_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self, scenario: str) -> dict[str, Any]:
        return {
            "scenario": scenario,
            "http_status": self.status,
            "terminal": self.terminal,
            "error": self.error,
            "output_types": [item["type"] for item in self.output],
            "event_types": sorted({event["type"] for event in self.events}),
            "request_id": self.request_id,
            "metadata": self.metadata,
        }


def sample(provider: str, key: str, history: list[dict[str, Any]]) -> Sample:
    base = os.environ["CLIPROXY_BASE_URL"].rstrip("/")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if provider == "gpt":
        path = "/responses"
        body = {
            "model": "gpt-6-luna",
            "instructions": "You solve synthetic arithmetic tasks. Tools are supplied by the caller.",
            "input": history,
            "stream": True,
            "store": False,
            "reasoning": {"effort": "high", "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": 4096,
            "tools": [{"type": "function", "name": "report_answer", "parameters": SCHEMA}],
        }
    else:
        path = "/messages"
        headers |= {"anthropic-version": "2023-06-01", "anthropic-beta": "thinking-binding-controls-2026-08-01"}
        body = {
            "model": "claude-haiku-5-5",
            "messages": history,
            "stream": True,
            "max_tokens": 4096,
            "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}},
            "output_config": {"effort": "high"},
            "tools": [{"name": "report_answer", "input_schema": SCHEMA}],
        }
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as error:
        return Sample(error.code, "http_error", error.read(8192).decode(errors="replace"), [], [], None)
    with response:
        request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            payload = json.load(response)
            return Sample(response.status, "non_stream", payload, [], [], request_id)
        events: list[dict[str, Any]] = []
        output: list[dict[str, Any]] = []
        blocks: dict[int, dict[str, Any]] = {}
        fragments: dict[int, str] = {}
        terminal = "eof"
        failure = None
        metadata: dict[str, Any] = {}
        byte_count = 0
        for line in response:
            byte_count += len(line)
            if byte_count > 1024 * 1024:
                raise ValueError("spike response exceeded 1 MiB")
            if not line.startswith(b"data:"):
                continue
            wire = line[5:].strip()
            if wire == b"[DONE]":
                break
            event = json.loads(wire)
            events.append(event)
            kind = event["type"]
            if kind == "response.output_item.done":
                output.append(event["item"])
            elif kind == "response.completed":
                completed = event["response"]
                metadata = {name: completed[name] for name in ("model", "usage", "status") if name in completed}
                terminal = "completed"
                break
            elif kind in {"response.failed", "response.incomplete", "error"}:
                terminal, failure = kind, event
                break
            elif kind == "content_block_start":
                blocks[event["index"]] = copy.deepcopy(event["content_block"])
            elif kind == "content_block_delta":
                index = event["index"]
                delta = event["delta"]
                for field in ("text", "thinking", "signature"):
                    if field in delta:
                        blocks[index][field] = blocks[index].get(field, "") + delta[field]
                if "partial_json" in delta:
                    fragments[index] = fragments.get(index, "") + delta["partial_json"]
            elif kind == "message_start":
                metadata = {
                    name: event["message"][name]
                    for name in ("model", "usage", "input_transformations")
                    if name in event["message"]
                }
            elif kind == "message_delta":
                metadata |= event["delta"]
                if "usage" in event:
                    metadata["usage"] = event["usage"]
            elif kind == "message_stop":
                for index, block in sorted(blocks.items()):
                    if index in fragments:
                        block["input"] = json.loads(fragments[index])
                    output.append(block)
                terminal = "completed"
                break
        return Sample(response.status, terminal, failure, output, events, request_id, metadata)


def reasoning_metadata(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"type": item["type"]}
    for name in ("encrypted_content", "signature"):
        if name in item:
            encoded = item[name].encode()
            result[name] = {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    return result


@pytest.mark.live_openai_api(
    credential_env="CLIPROXY_KEY_FILE" if "CLIPROXY_KEY_FILE" in os.environ else "CLIPROXY_CLIENT_KEY"
)
def test_reasoning_continuation() -> None:
    provider = os.environ["SPIKE_PROVIDER"]
    assert provider in {"gpt", "haiku"}
    if key_file := os.environ.get("CLIPROXY_KEY_FILE"):
        if "CLIPROXY_CLIENT_KEY" in os.environ:
            raise ValueError("Choose either a credential file or CLIPROXY_CLIENT_KEY")
        key = Path(key_file).read_text().strip()
    else:
        key = os.environ["CLIPROXY_CLIENT_KEY"]
    user = {"role": "user", "content": PROMPT}
    report: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "base_url": os.environ["CLIPROXY_BASE_URL"],
        "provider": provider,
        "requests": [],
    }
    destination = undeclared_outputs_dir() / f"{provider}.json"

    def probe(name: str, history: list[dict[str, Any]]) -> Sample:
        result = sample(provider, key, history)
        summary = result.summary(name)
        report["requests"].append(summary)
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(summary), flush=True)
        return result

    seed = probe("seed", [user])
    assert seed.terminal == "completed", seed.summary("seed")
    reasoning_type = "reasoning" if provider == "gpt" else "thinking"
    reasoning = [item for item in seed.output if item["type"] in {reasoning_type, "redacted_thinking"}]
    calls = [item for item in seed.output if item["type"] in {"function_call", "tool_use"}]
    assert reasoning, "seed did not produce reasoning; tail probes would be vacuous"
    assert calls, "seed did not produce the synthetic tool call"
    signature_field = "encrypted_content" if provider == "gpt" else "signature"
    assert all(item.get(signature_field) for item in reasoning), "seed did not return complete opaque reasoning"
    report["reasoning"] = [reasoning_metadata(item) for item in reasoning]
    report["seed_output_types"] = [item["type"] for item in seed.output]
    if provider == "gpt":
        full = [user, *seed.output]
        outputs = [
            {"type": "function_call_output", "call_id": call["call_id"], "output": "Recorded."} for call in calls
        ]
        tail = [user, *reasoning]
        notice = {"role": "developer", "content": NOTICE}
        initial = next(event["item"] for event in seed.events if event["type"] == "response.output_item.added")
        assert initial["type"] == "reasoning", "first added item was not reasoning"
        incomplete = [user, initial]
    else:
        full = [user, {"role": "assistant", "content": seed.output}]
        outputs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call["id"], "content": "Recorded."} for call in calls
                ],
            }
        ]
        tail = [user, {"role": "assistant", "content": reasoning}]
        notice = {"role": "user", "content": NOTICE}
        unsigned = copy.deepcopy(reasoning)
        for item in unsigned:
            item.pop("signature", None)
        incomplete = [user, {"role": "assistant", "content": unsigned}, notice]
    control = probe("complete_response_and_tool_result", [*full, *outputs])
    assert control.terminal == "completed", control.summary("control")
    probe("reasoning_only_tail", tail)
    probe("reasoning_then_recovery_notice", [*tail, notice])
    probe("incomplete_reasoning", incomplete)
    probe("unanswered_tool_call", full)
    discarded = probe("discard_response_and_add_notice", [user, notice])
    assert discarded.terminal == "completed", discarded.summary("discard")


if __name__ == "__main__":
    pytest_bazel.main()
