"""Fake OpenAI Responses API app for e2e testing.

Implements the OpenAI Responses API (/v1/responses) backed by mock objects
like SubprocessExecMock or StepRunner. Used in e2e tests where containers talk to
a real LLM proxy, which forwards to this fake upstream.

Requests are routed by the `model` field in the request body to the
corresponding mock in the dict.

Serve `create_app()` with `util.testing.asgi.bind_http_server()` and call
`check_errors()` after the serving context exits so mock exceptions fail the test
after server cleanup.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from openai.types.responses import (
    Response as OpenAIResponse,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    response_usage,
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
from props.testing.constants import DEFAULT_TEST_MODEL

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
                ResponseOutputText.model_validate(
                    {"type": "output_text", "text": part.text, "annotations": part.annotations or []}
                )
                for part in item.content
            ],
        )
    raise ValueError(f"Unexpected output item type: {type(item)}")


def result_to_sdk_response(result: ResponsesResult, *, model: str = DEFAULT_TEST_MODEL) -> OpenAIResponse:
    """Convert ResponsesResult to SDK Response object."""
    usage = result.usage
    if usage is None:
        usage = ResponseUsage(
            input_tokens=0,
            output_tokens=1,
            total_tokens=1,
            input_tokens_details=InputTokensDetails(cached_tokens=0, cache_write_tokens=0),
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
        usage=response_usage.ResponseUsage.model_validate(usage.model_dump()),
    )


class FakeOpenAIServer:
    """Fake OpenAI Responses API app for e2e testing.

    Routes requests to the mock matching the request's `model` field.

    Implements fail-fast error handling: exceptions from mocks are captured
    and re-raised when the caller invokes `check_errors()` after server cleanup.
    """

    def __init__(self, mocks: dict[str, OpenAIModelProto]) -> None:
        self._mocks = mocks
        self._captured_error: BaseException | None = None

    def _capture_error(self, error: BaseException) -> None:
        """Capture an error for later re-raising. Only captures the first error."""
        if self._captured_error is None:
            self._captured_error = error

    def check_errors(self) -> None:
        """Raise any captured error. Call this to fail fast on mock errors."""
        if self._captured_error is not None:
            raise self._captured_error

    def _resolve_mock(self, body: dict[str, Any]) -> OpenAIModelProto:
        """Resolve the mock for a request body by model name."""
        model = body.get("model")
        if not model:
            raise HTTPException(status_code=400, detail="model field required")
        mock = self._mocks.get(model)
        if mock is None:
            available = list(self._mocks.keys())
            raise HTTPException(status_code=400, detail=f"No mock for model '{model}'. Available: {available}")
        return mock

    def create_app(self) -> FastAPI:
        app = FastAPI(title="Fake OpenAI Server")

        @app.post("/v1/responses")
        async def responses(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

            mock = self._resolve_mock(body)

            try:
                req = ResponsesRequest.model_validate(body)
            except ValidationError as e:
                logger.warning("Failed to parse request: %s", e)
                raise HTTPException(status_code=400, detail=f"Invalid request: {e}")

            try:
                result = await mock.responses_create(req)
            except Exception as e:
                logger.exception("Mock raised exception")
                self._capture_error(e)
                raise HTTPException(status_code=500, detail=f"Mock error: {e}")

            sdk_response = result_to_sdk_response(result, model=body["model"])
            return JSONResponse(content=sdk_response.model_dump(mode="json"))

        return app
