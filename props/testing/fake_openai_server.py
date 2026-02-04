"""Fake OpenAI HTTP server for e2e testing.

Implements the OpenAI Responses API (/v1/responses) backed by mock objects
like PropsMock or StepRunner. Used in e2e tests where containers talk to
a real LLM proxy, which forwards to this fake upstream.

Accepts either a single mock (all requests go to it) or a dict of mocks
(requests are routed by the `model` field in the request body).

Usage:
    # Single mock - all requests handled by one mock
    mock = make_critic_mock()
    async with FakeOpenAIServer(mock) as server:
        ...

    # Multi-model - route by model name
    mocks = {"optimizer-model": opt_mock, "critic-model": crit_mock}
    async with FakeOpenAIServer(mocks) as server:
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
from openai.types.responses import (
    Response as OpenAIResponse,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from pydantic import ValidationError

from openai_utils.model import (
    AssistantMessageOut,
    FunctionCallItem,
    InputTokensDetails,
    OpenAIModelProto,
    OutputTokensDetails,
    ReasoningItem,
    ResponseOutItem,
    ResponsesRequest,
    ResponsesResult,
    ResponseUsage,
)

logger = logging.getLogger(__name__)

# SDK output item type union
_SDKOutputItem = ResponseOutputMessage | ResponseFunctionToolCall | ResponseReasoningItem


def _to_sdk_output_item(item: ResponseOutItem) -> _SDKOutputItem:
    """Convert internal output item to SDK Response output model.

    FunctionCallItem and ReasoningItem fields match the SDK closely, so
    model_dump() + model_validate() works with minimal fixups.
    AssistantMessageOut uses different field names and needs explicit construction.
    """
    if isinstance(item, FunctionCallItem):
        return ResponseFunctionToolCall.model_validate(
            item.model_dump() | {"id": item.id or f"fc_{item.call_id}", "arguments": item.arguments or "{}"}
        )
    if isinstance(item, ReasoningItem):
        return ResponseReasoningItem.model_validate(item.model_dump() | {"id": item.id or "rs_test"})
    if isinstance(item, AssistantMessageOut):
        return ResponseOutputMessage(
            type="message",
            role="assistant",
            id=item.id or "msg_test",
            status="completed",
            content=[
                ResponseOutputText(type="output_text", text=part.text, annotations=part.annotations or [])
                for part in item.content
            ],
        )
    raise ValueError(f"Unexpected output item type: {type(item)}")


def result_to_sdk_response(result: ResponsesResult, *, model: str = "test-model") -> OpenAIResponse:
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

    return OpenAIResponse(
        id=result.id,
        object="response",
        created_at=0,
        model=model,
        status="completed",
        output=[_to_sdk_output_item(item) for item in result.output],
        parallel_tool_calls=True,
        tool_choice="auto",
        tools=[],
        usage=usage.model_dump(),
    )


class FakeOpenAIServer:
    """Fake OpenAI Responses API server for e2e testing.

    Accepts a single mock or a dict of mocks keyed by model name.
    When given a dict, routes requests to the mock matching the request's
    `model` field.

    Implements fail-fast error handling: exceptions from mocks are captured
    and re-raised when the server is stopped or when check_errors() is called.
    """

    def __init__(
        self, mock: OpenAIModelProto | dict[str, OpenAIModelProto], host: str = "127.0.0.1", port: int = 0
    ) -> None:
        self._mock = mock
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

    def _resolve_mock(self, body: dict[str, Any]) -> OpenAIModelProto:
        """Resolve the mock for a request body."""
        if isinstance(self._mock, dict):
            model = body.get("model")
            if not model:
                raise HTTPException(status_code=400, detail="model field required")
            mock = self._mock.get(model)
            if mock is None:
                available = list(self._mock.keys())
                raise HTTPException(status_code=400, detail=f"No mock for model '{model}'. Available: {available}")
            return mock
        return self._mock

    def _create_app(self) -> FastAPI:
        app = FastAPI(title="Fake OpenAI Server")
        server_self = self

        @app.post("/v1/responses")
        async def responses(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

            mock = server_self._resolve_mock(body)

            try:
                req = ResponsesRequest.model_validate(body)
            except ValidationError as e:
                logger.warning("Failed to parse request: %s", e)
                raise HTTPException(status_code=400, detail=f"Invalid request: {e}")

            try:
                result = await mock.responses_create(req)
            except Exception as e:
                logger.exception("Mock raised exception")
                server_self._capture_error(e)
                raise HTTPException(status_code=500, detail=f"Mock error: {e}")

            request_model = body.get("model", "test-model")
            sdk_response = result_to_sdk_response(result, model=request_model)
            return JSONResponse(content=sdk_response.model_dump(mode="json"))

        return app

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

        logger.info("FakeOpenAIServer started on %s", self.url)

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
            logger.info("FakeOpenAIServer stopped")
        # Re-raise any captured mock errors so tests fail visibly
        self.check_errors()

    async def __aenter__(self) -> FakeOpenAIServer:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        await self.stop()
