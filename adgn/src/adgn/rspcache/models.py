from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, cast

from openai.types.responses import (
    Response as OpenAIResponse,
    ResponseError,
    ResponseStreamEvent,
    ResponseUsage,
)
from pydantic import BaseModel, ConfigDict, TypeAdapter, field_serializer

FRAME_ADAPTER: TypeAdapter[ResponseStreamEvent] = TypeAdapter(ResponseStreamEvent)
RESPONSE_ADAPTER: TypeAdapter[OpenAIResponse] = TypeAdapter(OpenAIResponse)
ERROR_ADAPTER: TypeAdapter[ResponseError] = TypeAdapter(ResponseError)
USAGE_ADAPTER: TypeAdapter[ResponseUsage] = TypeAdapter(ResponseUsage)


class ResponseStatus(StrEnum):
    COMPLETE = "complete"
    ERROR = "error"


class ErrorPayload(BaseModel):
    """Lightweight proxy error payload captured by the rspcache proxy."""

    message: str | None = None
    code: str | None = None
    detail: Any | None = None

    model_config = ConfigDict(extra="allow")


class FinalResponseSnapshot(BaseModel):
    """Canonical representation of a completed or errored response."""

    status: ResponseStatus
    response: OpenAIResponse | None = None
    error: ErrorPayload | None = None
    token_usage: ResponseUsage | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_serializer('status')
    def serialize_status(self, value: ResponseStatus) -> str:
        return value.value

    @classmethod
    def from_db(
        cls,
        *,
        status: str,
        response: Any,
        error: Any,
        token_usage: Any,
    ) -> FinalResponseSnapshot:
        return cls(
            status=ResponseStatus(status),
            response=parse_response(response) if response is not None else None,
            error=parse_error(error) if error is not None else None,
            token_usage=parse_usage(token_usage) if token_usage is not None else None,
        )


def stream_event_event_id(event: ResponseStreamEvent) -> str | None:
    payload = event.model_dump(mode="python")
    event_id = payload.get("event_id")
    return event_id if isinstance(event_id, str) else None


def stream_event_response_id(event: ResponseStreamEvent) -> str | None:
    payload = event.model_dump(mode="python")
    response_id = payload.get("response_id")
    if isinstance(response_id, str):
        return response_id
    response = payload.get("response")
    if isinstance(response, Mapping):
        value = response.get("id")
        if isinstance(value, str):
            return value
    return None


def stream_event_usage(event: ResponseStreamEvent) -> ResponseUsage | None:
    payload = event.model_dump(mode="python")
    usage_candidate = payload.get("usage")
    if isinstance(usage_candidate, ResponseUsage):
        return usage_candidate
    if isinstance(usage_candidate, Mapping):
        return parse_usage(usage_candidate)
    response = payload.get("response")
    if isinstance(response, Mapping):
        usage_value = response.get("usage")
        if isinstance(usage_value, ResponseUsage):
            return usage_value
        if isinstance(usage_value, Mapping):
            return parse_usage(usage_value)
    return None


def stream_event_final_response(event: ResponseStreamEvent) -> OpenAIResponse | None:
    payload = event.model_dump(mode="python")
    response_payload = payload.get("response")
    if response_payload is not None:
        return parse_response(response_payload)
    return None


def parse_response(value: OpenAIResponse | Mapping[str, object]) -> OpenAIResponse:
    if value is None:
        raise ValueError("response payload cannot be None")
    if isinstance(value, OpenAIResponse):
        return value
    return RESPONSE_ADAPTER.validate_python(value)


def parse_error(value: Any) -> ErrorPayload:
    if value is None:
        return ErrorPayload()
    if isinstance(value, ErrorPayload):
        return value
    if isinstance(value, ResponseError):
        return ErrorPayload.model_validate(value.model_dump(mode="json"))
    if not isinstance(value, dict):
        raise ValueError("error payload must be a mapping")
    return ErrorPayload.model_validate(value)


def parse_usage(value: ResponseUsage | Mapping[str, object]) -> ResponseUsage:
    if isinstance(value, ResponseUsage):
        return value
    return USAGE_ADAPTER.validate_python(value)
