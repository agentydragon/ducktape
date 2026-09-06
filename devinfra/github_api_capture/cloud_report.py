"""Metadata for Claude's cloud-mediated GitHub routes, not upstream GitHub cost."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from mitmproxy import http

PATHS = frozenset(
    {
        "/v1/code/github/batch-branch-status",
        "/v1/code/github/compare-refs",
        "/v1/code/github/org-connection/installations-status",
    }
)


@dataclass(frozen=True)
class CloudRequestRecord:
    started_at: str
    completed_at: str | None
    host: str
    path: str
    method: str
    caller: str | None
    status: int | None
    request_sha256: str
    repo_branch_count: int | None
    session_count: int | None
    transport_error: bool


def summarize(flow: http.HTTPFlow) -> CloudRequestRecord | None:
    path = flow.request.path.split("?", 1)[0]
    if flow.request.host != "claude.ai" or path not in PATHS:
        return None
    branches = None
    sessions = None
    if path == "/v1/code/github/batch-branch-status" and flow.request.method == "POST":
        try:
            payload = json.loads(flow.request.get_text() or "null")
        except json.JSONDecodeError:
            raise ValueError("Cloud GitHub request body is not JSON") from None
        if not isinstance(payload, dict):
            raise ValueError("Cloud GitHub request body is not a JSON object")
        if isinstance(payload.get("repo_branches"), list):
            branches = len(payload["repo_branches"])
        if isinstance(payload.get("session_ids"), list):
            sessions = len(payload["session_ids"])
    caller = flow.request.query.get("caller")
    if caller is not None and not re.fullmatch(r"[A-Za-z0-9_:.-]{1,128}", caller):
        caller = None
    return CloudRequestRecord(
        started_at=datetime.fromtimestamp(flow.request.timestamp_start, UTC).isoformat(),
        completed_at=(
            datetime.fromtimestamp(flow.response.timestamp_end, UTC).isoformat()
            if flow.response is not None and flow.response.timestamp_end is not None
            else None
        ),
        host=flow.request.host,
        path=path,
        method=flow.request.method,
        caller=caller,
        status=flow.response.status_code if flow.response is not None else None,
        request_sha256=hashlib.sha256(flow.request.raw_content or b"").hexdigest(),
        repo_branch_count=branches,
        session_count=sessions,
        transport_error=flow.error is not None,
    )


def response(flow: http.HTTPFlow) -> None:
    if record := summarize(flow):
        print(json.dumps(asdict(record)), flush=True)


def error(flow: http.HTTPFlow) -> None:
    response(flow)
