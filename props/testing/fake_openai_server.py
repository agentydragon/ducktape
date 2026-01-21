"""Fake OpenAI HTTP server for e2e testing.

Implements the OpenAI Responses API (/v1/responses) backed by mock objects
like PropsMock or StepRunner. Used in e2e tests where containers talk to
a real LLM proxy, which forwards to this fake upstream.

Usage:
    mock = make_critic_mock()
    async with FakeOpenAIServer(mock) as server:
        # server.url is e.g. "http://127.0.0.1:8765"
        # Configure proxy's OPENAI_UPSTREAM_URL to point here
        ...
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from openai.types.responses import Response as OpenAIResponse
from pydantic import TypeAdapter, ValidationError

from openai_utils.model import (
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    InputTokensDetails,
    OpenAIModelProto,
    OutputTokensDetails,
    ReasoningItem,
    ResponsesRequest,
    ResponsesResult,
    ResponseUsage,
)

logger = logging.getLogger(__name__)

# TypeAdapter for SDK Response (for validation/construction)
_openai_response_adapter: TypeAdapter[OpenAIResponse] = TypeAdapter(OpenAIResponse)


def _output_item_to_sdk_dict(
    item: AssistantMessageOut | FunctionCallItem | FunctionCallOutputItem | ReasoningItem,
) -> dict[str, Any]:
    """Convert internal output item to SDK Response output format."""
    if isinstance(item, AssistantMessageOut):
        return {
            "type": "message",
            "role": "assistant",
            "id": item.id or "msg_test",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": part.text, "annotations": part.annotations or []} for part in item.parts
            ],
        }
    if isinstance(item, FunctionCallItem):
        return {
            "type": "function_call",
            "id": item.id or f"fc_{item.call_id}",
            "call_id": item.call_id,
            "name": item.name,
            "arguments": item.arguments or "{}",
            "status": item.status or "completed",
        }
    if isinstance(item, FunctionCallOutputItem):
        # Function call outputs typically appear as input items, but may be echoed in output
        output_str = item.output if isinstance(item.output, str) else str(item.output)
        return {
            "type": "function_call_output",
            "id": f"fco_{item.call_id}",
            "call_id": item.call_id,
            "output": output_str,
        }
    if isinstance(item, ReasoningItem):
        return {
            "type": "reasoning",
            "id": item.id or "rs_test",
            "summary": [{"type": s.type, "text": s.text} for s in item.summary],
        }
    raise ValueError(f"Unknown output item type: {type(item)}")


def result_to_sdk_response(result: ResponsesResult) -> OpenAIResponse:
    """Convert ResponsesResult to SDK Response object."""
    usage = result.usage
    if usage is None:
        usage = ResponseUsage(
            input_tokens=0,
            output_tokens=1,
            total_tokens=1,
            input_tokens_details=InputTokensDetails(cached_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        )

    # Build SDK-compatible dict and validate into Response
    response_dict = {
        "id": result.id,
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "output": [_output_item_to_sdk_dict(item) for item in result.output],
        "parallel_tool_calls": True,
        "usage": usage.model_dump(),
    }
    return _openai_response_adapter.validate_python(response_dict)


class _BaseServer:
    """Base class for fake OpenAI servers with shared uvicorn lifecycle.

    Implements fail-fast error handling: exceptions from mocks are captured
    and re-raised when the server is stopped or when check_errors() is called.
    This ensures test failures are highly visible rather than being silently
    converted to HTTP 500 responses.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._actual_port: int | None = None
        self._captured_error: BaseException | None = None

    @property
    def url(self) -> str:
        if self._actual_port is None:
            raise RuntimeError("Server not started")
        return f"http://{self._host}:{self._actual_port}"

    @property
    def port(self) -> int:
        if self._actual_port is None:
            raise RuntimeError("Server not started")
        return self._actual_port

    def _capture_error(self, error: BaseException) -> None:
        """Capture an error for later re-raising. Only captures the first error."""
        if self._captured_error is None:
            self._captured_error = error

    def check_errors(self) -> None:
        """Raise any captured error. Call this to fail fast on mock errors."""
        if self._captured_error is not None:
            raise self._captured_error

    def _create_app(self) -> FastAPI:
        raise NotImplementedError

    async def start(self) -> None:
        app = self._create_app()
        config = uvicorn.Config(app, host=self._host, port=self._port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())

        while not self._server.started:
            await asyncio.sleep(0.01)
            if self._task.done():
                exc = self._task.exception()
                raise RuntimeError(f"Server failed to start: {exc}")

        for server in self._server.servers:
            for socket in server.sockets:
                self._actual_port = socket.getsockname()[1]
                break
            break

        logger.info("%s started on %s", self.__class__.__name__, self.url)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            if self._task is not None:
                try:
                    await asyncio.wait_for(self._task, timeout=5.0)
                except TimeoutError:
                    self._task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._task
            logger.info("%s stopped", self.__class__.__name__)
        # Re-raise any captured mock errors so tests fail visibly
        self.check_errors()

    async def __aenter__(self) -> _BaseServer:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        await self.stop()


class FakeOpenAIServer(_BaseServer):
    """HTTP server that serves mock OpenAI Responses API.

    Wraps a mock implementing OpenAIModelProto and exposes it via HTTP.
    Use as async context manager to start/stop the server.

    Example:
        @PropsMock.mock()
        def mock(m: PropsMock):
            yield None
            yield from m.docker_exec_roundtrip(["critique", "submit", "0", "Done"])

        async with FakeOpenAIServer(mock) as server:
            # server.url is the base URL (e.g., "http://127.0.0.1:8765")
            # Configure OPENAI_UPSTREAM_URL to point here
            ...
    """

    def __init__(self, mock: OpenAIModelProto, host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__(host, port)
        self._mock = mock

    def _create_app(self) -> FastAPI:
        app = FastAPI(title="Fake OpenAI Server")
        mock = self._mock
        capture_error = self._capture_error

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/v1/responses")
        async def responses(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

            try:
                req = ResponsesRequest.model_validate(body)
            except ValidationError as e:
                logger.warning("Failed to parse request: %s", e)
                raise HTTPException(status_code=400, detail=f"Invalid request: {e}")

            try:
                result = await mock.responses_create(req)
            except Exception as e:
                logger.exception("Mock raised exception")
                capture_error(e)
                raise HTTPException(status_code=500, detail=f"Mock error: {e}")

            # Convert to SDK Response and serialize
            sdk_response = result_to_sdk_response(result)
            return JSONResponse(content=sdk_response.model_dump(mode="json"))

        return app

    async def __aenter__(self) -> FakeOpenAIServer:
        await self.start()
        return self


class MultiModelFakeOpenAI(_BaseServer):
    """Fake OpenAI server that routes requests to different mocks by model.

    Useful for testing prompt optimizers that spawn critics and graders
    with different model strings.

    Example:
        mocks = {
            "gpt-5-optimizer": optimizer_mock,
            "gpt-4o-critic": critic_mock,
            "gpt-4o-grader": grader_mock,
        }
        async with MultiModelFakeOpenAI(mocks) as server:
            ...
    """

    def __init__(self, mocks: dict[str, OpenAIModelProto], host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__(host, port)
        self._mocks = mocks

    def _create_app(self) -> FastAPI:
        app = FastAPI(title="Multi-Model Fake OpenAI Server")
        mocks = self._mocks
        capture_error = self._capture_error

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/v1/responses")
        async def responses(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

            model = body.get("model")
            if not model:
                raise HTTPException(status_code=400, detail="model field required")

            mock = mocks.get(model)
            if mock is None:
                available = list(mocks.keys())
                raise HTTPException(status_code=400, detail=f"No mock for model '{model}'. Available: {available}")

            try:
                req = ResponsesRequest.model_validate(body)
            except ValidationError as e:
                raise HTTPException(status_code=400, detail=f"Invalid request: {e}")

            try:
                result = await mock.responses_create(req)
            except Exception as e:
                logger.exception("Mock raised exception for model %s", model)
                capture_error(e)
                raise HTTPException(status_code=500, detail=f"Mock error: {e}")

            # Convert to SDK Response and serialize
            sdk_response = result_to_sdk_response(result)
            return JSONResponse(content=sdk_response.model_dump(mode="json"))

        return app

    async def __aenter__(self) -> MultiModelFakeOpenAI:
        await self.start()
        return self
