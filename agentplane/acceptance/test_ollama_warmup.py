"""The Ollama warm-up asks what is loaded and loads the model only when it is not."""

import json

import httpx
import pytest
import pytest_bazel

from agentplane.acceptance.ollama_warmup import warm_ollama
from model_catalog.catalog import OLLAMA_QWEN_IQ4XS_128K

QWEN = OLLAMA_QWEN_IQ4XS_128K.native


def ollama(loaded: list[str], seen: list[httpx.Request]) -> httpx.AsyncBaseTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"models": [{"name": name} for name in loaded]})

    return httpx.MockTransport(handle)


async def test_an_unloaded_model_is_loaded_with_its_context_size() -> None:
    seen: list[httpx.Request] = []
    await warm_ollama(QWEN.id, transport=ollama([], seen))
    assert [(request.method, request.url.path) for request in seen] == [("GET", "/api/ps"), ("POST", "/api/generate")]
    body = json.loads(seen[1].content)
    assert (body["model"], body["prompt"], body["options"]) == (QWEN.upstream_model, "", {"num_ctx": QWEN.num_ctx})


async def test_a_loaded_model_is_left_alone() -> None:
    seen: list[httpx.Request] = []
    assert QWEN.upstream_model is not None
    await warm_ollama(QWEN.id, transport=ollama([QWEN.upstream_model], seen))
    assert [request.url.path for request in seen] == ["/api/ps"]


async def test_a_model_ollama_does_not_serve_is_not_asked_about() -> None:
    seen: list[httpx.Request] = []
    await warm_ollama("chatgpt/oai-responses/gpt-6-luna", transport=ollama([], seen))
    assert seen == []


async def test_an_unreachable_ollama_does_not_fail_the_test(caplog: pytest.LogCaptureFixture) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    await warm_ollama(QWEN.id, transport=httpx.MockTransport(refuse))
    assert any(record.levelname == "WARNING" for record in caplog.records)


if __name__ == "__main__":
    pytest_bazel.main()
