"""mitmproxy addon for GitHub-only request metadata, live or from a saved capture."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from mitmproxy import http


@dataclass(frozen=True)
class GraphqlError:
    type: str | None
    code: str | None


@dataclass(frozen=True)
class RequestRecord:
    started_at: str
    completed_at: str | None
    user_agent: str | None
    method: str
    path: str
    status: int | None
    query_sha256: str | None
    nominal_graphql_cost: int | None
    graphql_errors: list[GraphqlError] | None
    github_request_id: str | None
    account_rate_resource: str | None
    account_rate_used: int | None
    account_rate_remaining: int | None
    account_rate_reset: int | None
    transport_error: bool


def _json_object(content: str | None) -> dict[str, Any]:
    # Include no input in errors: captures can contain credentials and private data.
    if content is None:
        raise ValueError("GitHub GraphQL body is unavailable")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        raise ValueError("GitHub GraphQL body is not JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("GitHub GraphQL body is not a JSON object")
    return payload


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:.-]{1,128}", value) else None


def _header_integer(value: str | None) -> int | None:
    return int(value) if value is not None and re.fullmatch(r"[0-9]{1,20}", value) else None


def summarize(flow: http.HTTPFlow) -> RequestRecord | None:
    if flow.request.host != "api.github.com":
        return None
    path = flow.request.path.split("?", 1)[0]
    query = None
    cost = None
    errors = None
    if path == "/graphql":
        query = (
            flow.request.query.get("query")
            if flow.request.method == "GET"
            else _json_object(flow.request.get_text()).get("query")
        )
        if query is not None and not isinstance(query, str):
            raise ValueError("GitHub GraphQL query is not a string")
        if flow.response is not None and "json" in flow.response.headers.get("content-type", ""):
            payload = _json_object(flow.response.get_text())
            errors = []
            for error in payload.get("errors") or []:
                if not isinstance(error, dict):
                    raise ValueError("GitHub GraphQL error is not a JSON object")
                errors.append(GraphqlError(type=_identifier(error.get("type")), code=_identifier(error.get("code"))))
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("rateLimit"), dict):
                cost = data["rateLimit"].get("cost")
                if cost is not None and (type(cost) is not int or cost < 0):
                    raise ValueError("GitHub GraphQL cost is not a nonnegative integer")
    headers = flow.response.headers if flow.response is not None else http.Headers()
    return RequestRecord(
        started_at=datetime.fromtimestamp(flow.request.timestamp_start, UTC).isoformat(),
        completed_at=(
            datetime.fromtimestamp(flow.response.timestamp_end, UTC).isoformat()
            if flow.response is not None and flow.response.timestamp_end is not None
            else None
        ),
        user_agent=flow.request.headers.get("user-agent"),
        method=flow.request.method,
        path=path,
        status=flow.response.status_code if flow.response is not None else None,
        query_sha256=hashlib.sha256(query.encode()).hexdigest() if query is not None else None,
        nominal_graphql_cost=cost,
        graphql_errors=errors,
        github_request_id=_identifier(headers.get("x-github-request-id")),
        account_rate_resource=headers.get("x-ratelimit-resource"),
        account_rate_used=_header_integer(headers.get("x-ratelimit-used")),
        account_rate_remaining=_header_integer(headers.get("x-ratelimit-remaining")),
        account_rate_reset=_header_integer(headers.get("x-ratelimit-reset")),
        transport_error=flow.error is not None,
    )


def response(flow: http.HTTPFlow) -> None:
    if record := summarize(flow):
        print(json.dumps(asdict(record)), flush=True)


def error(flow: http.HTTPFlow) -> None:
    response(flow)
