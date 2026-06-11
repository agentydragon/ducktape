"""Pydantic models for test reference data structure."""

from typing import Any

from pydantic import BaseModel


class RequestData(BaseModel):
    """Test reference request data."""

    method: str
    url: str
    headers: dict[str, str] = {}
    body: dict[str, Any] | None = None


class ResponseData(BaseModel):
    """Test reference response data."""

    status_code: int = 200
    headers: dict[str, str] = {}
    json_data: dict[str, Any] | None = None
    text: str | None = None


class TestReference(BaseModel):
    """Complete test reference data structure."""

    name: str
    request: RequestData
    response: ResponseData
