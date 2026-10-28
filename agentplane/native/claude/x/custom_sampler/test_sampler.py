"""Custom sampler -> pinned proxy -> scripted provider; executor lives in another process."""

import asyncio
import base64
import json
import os
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel
from aiohttp import web
from aiohttp.test_utils import TestServer

from agentplane.native.claude.testing.anthropic_sse import Text, Thinking, ToolUse, message_stream
from agentplane.native.claude.x.custom_sampler.conftest import MODEL, Provider
from agentplane.native.claude.x.custom_sampler.sampler import ProviderError, Sampler
from agentplane.native.testing.model_endpoint import JsonResponse, SseEvent

# Syntactically opaque fixture data, not a provider-issued signature or eligibility proof.
_channel = b"\x08\x0c\x10\x02\x32" + bytes([len(MODEL)]) + MODEL.encode()
_container = b"\x0a" + bytes([len(_channel)]) + _channel
SIGNATURE = base64.b64encode(b"\x12" + bytes([len(_container)]) + _container + b"\x18\x01").decode()


@pytest.mark.parametrize("route", ["direct", "profile"])
async def test_tool_continuation(route: str, provider: Provider, proxy: str, executor: str, workspace: Path) -> None:
    sampler = Sampler(
        proxy if route == "profile" else provider.origin + "/v1/messages", executor, MODEL, "sampler-test-client"
    )
    async with asyncio.TaskGroup() as tasks:
        run = tasks.create_task(sampler.run("Append hello to result.txt", max_requests=3))
        async with await provider.next() as exchange:
            first = provider.requests[-1]
            upstream_name = first["tools"][-1]["name"]
            if route == "profile":
                assert upstream_name.startswith("mcp__")
                assert provider.headers[-1]["X-App"] == "cli"
                assert provider.headers[-1]["User-Agent"].startswith("claude-cli/")
                assert "claude-code-20250219" in provider.headers[-1]["Anthropic-Beta"]
                assert "device_id" in json.loads(first["metadata"]["user_id"])
                assert any("cache_control" in block for block in first["system"])
                assert any("You are Claude Code" in block["text"] for block in first["system"])
            else:
                assert upstream_name == "append_file"
                assert "X-App" not in provider.headers[-1]
            stream = message_stream(
                [
                    Thinking("opaque reasoning", SIGNATURE),
                    ToolUse("toolu_append", upstream_name, {"path": "result.txt", "text": "hello\n"}),
                ],
                model=MODEL,
            )
            await exchange.send(*stream.events)
        async with await provider.next() as exchange:
            continuation = provider.requests[-1]
            if route == "profile":
                assert continuation["metadata"]["user_id"] == first["metadata"]["user_id"]
                assert continuation["system"] == first["system"]
            assistant = next(message for message in continuation["messages"] if message["role"] == "assistant")
            assert assistant["content"][0] == {
                "type": "thinking",
                "thinking": "opaque reasoning",
                "signature": SIGNATURE,
            }
            assert assistant["content"][1]["name"] == upstream_name
            result = continuation["messages"][-1]["content"][0]
            assert result["tool_use_id"] == "toolu_append"
            receipt = json.loads(result["content"])
            assert receipt["appended"] == 6
            assert receipt["executor_pid"] != os.getpid()
            child_environment = await asyncio.to_thread(Path(f"/proc/{receipt['executor_pid']}/environ").read_bytes)
            assert b"sampler-test-client" not in child_environment
            assert (workspace / "result.txt").read_text() == "hello\n"
            await exchange.send(*message_stream([Text("Appended hello.")], model=MODEL).events)
    assert run.result()["content"] == [{"type": "text", "text": "Appended hello."}]
    assert run.result()["usage"]["output_tokens"] == 12
    assert sampler.messages[1]["content"][1]["name"] == "append_file"
    assert len(provider.requests) == 2


@pytest.mark.parametrize("kind", ["redacted_thinking", "future_provider_block"])
async def test_complete_unknown_blocks_survive(kind: str, provider: Provider, executor: str) -> None:
    sampler = Sampler(provider.origin, executor, MODEL, "sampler-test-client")
    opaque = {"type": kind, "data": "opaque-provider-ciphertext", "extension": {"future": [1, 2]}}
    stream = message_stream([ToolUse("toolu_opaque", "append_file", {"path": "opaque.txt", "text": "ok"})], model=MODEL)
    extra = [
        SseEvent(
            "content_block_start",
            f"data: {json.dumps({'type': 'content_block_start', 'index': 1, 'content_block': opaque})}\n\n".encode(),
        ),
        SseEvent("content_block_stop", b'data: {"type":"content_block_stop","index":1}\n\n'),
    ]
    async with asyncio.TaskGroup() as tasks:
        run = tasks.create_task(sampler.run("append", max_requests=2))
        async with await provider.next() as exchange:
            await exchange.send(*stream.events[:-2], *extra, *stream.events[-2:])
        async with await provider.next() as exchange:
            assert provider.requests[-1]["messages"][1]["content"][1] == opaque
            await exchange.send(*message_stream([Text("done")], model=MODEL).events)
    assert run.result()["stop_reason"] == "end_turn"


@pytest.mark.parametrize("failure", ["http", "sse", "truncated"])
async def test_provider_failure_never_executes_partial_tool(
    failure: str, provider: Provider, executor: str, workspace: Path
) -> None:
    sampler = Sampler(provider.origin, executor, MODEL, "sampler-test-client")

    async def run() -> None:
        with pytest.raises((ProviderError, aiohttp.ClientResponseError)):
            await sampler.run("append", max_requests=2)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(run())
        async with await provider.next() as exchange:
            if failure == "http":
                await exchange.respond(JsonResponse(b'{"error":{"type":"overloaded_error"}}', status=503))
            else:
                stream = message_stream(
                    [ToolUse("toolu_partial", "append_file", {"path": "partial.txt", "text": "bad"})], model=MODEL
                )
                await exchange.send(*stream.through("content_block_stop").events)
                if failure == "sse":
                    await exchange.send(
                        SseEvent("error", b'data: {"type":"error","error":{"type":"overloaded_error"}}\n\n')
                    )
                await exchange.close()
    assert not (workspace / "partial.txt").exists()
    assert sampler.messages == [{"role": "user", "content": "append"}]
    assert len(provider.requests) == 1


async def test_interrupt_during_sampling(provider: Provider, executor: str, workspace: Path) -> None:
    sampler = Sampler(provider.origin, executor, MODEL, "sampler-test-client")
    async with asyncio.TaskGroup() as tasks:
        run = tasks.create_task(sampler.run("append", max_requests=2))
        async with await provider.next() as exchange:
            stream = message_stream(
                [ToolUse("toolu_interrupt", "append_file", {"path": "interrupt.txt", "text": "bad"})], model=MODEL
            )
            await exchange.send(*stream.through("input_json_delta").events)
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
            await exchange.wait_client_closed()
    assert not (workspace / "interrupt.txt").exists()
    assert len(sampler.messages) == 1


async def test_interrupt_after_effect_before_receipt(provider: Provider, executor: str, workspace: Path) -> None:
    effected = asyncio.Event()
    release = asyncio.Event()

    async def hold_receipt(request: web.Request) -> web.Response:
        async with aiohttp.ClientSession() as client, client.post(executor, json=await request.json()) as response:
            response.raise_for_status()
            receipt = await response.json()
        effected.set()
        await release.wait()
        return web.json_response(receipt)

    app = web.Application()
    app.router.add_post("/append", hold_receipt)
    async with TestServer(app) as held:
        sampler = Sampler(provider.origin, str(held.make_url("/append")), MODEL, "sampler-test-client")
        try:
            async with asyncio.TaskGroup() as tasks:
                run = tasks.create_task(sampler.run("append", max_requests=2))
                async with await provider.next() as exchange:
                    await exchange.send(
                        *message_stream(
                            [ToolUse("toolu_uncertain", "append_file", {"path": "uncertain.txt", "text": "once\n"})],
                            model=MODEL,
                        ).events
                    )
                async with asyncio.timeout(5):
                    await effected.wait()
                run.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await run
            assert (workspace / "uncertain.txt").read_text() == "once\n"
            assert sampler.messages[-1]["role"] == "assistant"  # Effect exists; its result was never received.
            with pytest.raises(ValueError, match="effect audit"):
                await sampler.run("retry", max_requests=2)
            assert len(provider.requests) == 1
        finally:
            release.set()


@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/outside.txt", "escape/outside.txt"])
async def test_executor_rejects_path_escape(path: str, executor: str, workspace: Path) -> None:
    (workspace / "escape").symlink_to(workspace.parent, target_is_directory=True)
    async with aiohttp.ClientSession() as client, client.post(executor, json={"path": path, "text": "bad"}) as response:
        assert response.status == 403
    assert not (workspace.parent / "outside.txt").exists()


if __name__ == "__main__":
    pytest_bazel.main()
