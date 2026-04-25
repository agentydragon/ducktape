"""Commit-msg hook: verify commit messages contain a BAZEL_TEST_INVOCATIONS= tag.

Enforces that every commit documents its test coverage via a BAZEL_TEST_INVOCATIONS= line:
  BAZEL_TEST_INVOCATIONS=buildbuddy:<uuid>                     single BuildBuddy test invocation
  BAZEL_TEST_INVOCATIONS=buildbuddy:<uuid>,local:<uuid>        comma-separated, mixed sources
  BAZEL_TEST_INVOCATIONS=none: <explanation>                   no tests affected, with rationale

buildbuddy: invocations are verified against the BuildBuddy API.
local: invocations are accepted without verification.

Gated by DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG=1 (off by default).
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass

import httpx
import tenacity
from google.protobuf import json_format
from proto import invocation_pb2

logger = logging.getLogger(__name__)
_TAG_PATTERN = re.compile(r"^BAZEL_TEST_INVOCATIONS=(.*)$", re.MULTILINE)
_EXEMPT_PREFIXES = ("Merge ", "fixup! ", "squash! ")
_NONE_PREFIX = "none:"
_BUILDBUDDY_API_URL = "https://app.buildbuddy.io/rpc/BuildBuddyService/GetInvocation"

_MISSING_TAG_MESSAGE = """\
Commit message must contain a BAZEL_TEST_INVOCATIONS= tag.

Run affected tests and add one of:
  BAZEL_TEST_INVOCATIONS=buildbuddy:<uuid>        BuildBuddy test invocation (comma-separated for multiple)
  BAZEL_TEST_INVOCATIONS=local:<uuid>             local test invocation (not verified)
  BAZEL_TEST_INVOCATIONS=none: <explanation>      when no tests are affected, with rationale

Example:
  BAZEL_TEST_INVOCATIONS=buildbuddy:abc12345-1234-5678-9abc-def012345678
  BAZEL_TEST_INVOCATIONS=none: documentation-only change"""


_RETRIABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class TestTagError(Exception):
    """Raised when a commit message has a missing or invalid BAZEL_TEST_INVOCATIONS= tag."""


class _RetriableHTTPError(Exception):
    """Raised on HTTP status codes that warrant a retry (429, 5xx)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@dataclass(frozen=True)
class BuildBuddyInvocation:
    id: uuid.UUID


@dataclass(frozen=True)
class LocalInvocation:
    id: uuid.UUID


@dataclass(frozen=True)
class Invocations:
    items: list[BuildBuddyInvocation | LocalInvocation]


@dataclass(frozen=True)
class NoTests:
    explanation: str


TestTag = Invocations | NoTests

_SOURCE_PARSERS: dict[str, type[BuildBuddyInvocation | LocalInvocation]] = {
    "buildbuddy": BuildBuddyInvocation,
    "local": LocalInvocation,
}


def is_exempt(message: str) -> bool:
    return any(message.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _parse_invocation_ref(raw: str) -> BuildBuddyInvocation | LocalInvocation:
    """Parse 'source:uuid' into a typed invocation. Raises TestTagError."""
    if ":" not in raw:
        raise TestTagError(f"Invalid invocation reference (expected 'buildbuddy:<uuid>' or 'local:<uuid>'): {raw}")
    source, _, id_str = raw.partition(":")
    cls = _SOURCE_PARSERS.get(source)
    if cls is None:
        raise TestTagError(f"Unknown invocation source '{source}' (expected 'buildbuddy' or 'local'): {raw}")
    try:
        return cls(id=uuid.UUID(id_str))
    except ValueError:
        raise TestTagError(f"Invalid UUID in invocation reference: {raw}")


def parse_test_tag(message: str) -> TestTag:
    """Parse BAZEL_TEST_INVOCATIONS= tag from commit message. Raises TestTagError if missing or malformed."""
    match = _TAG_PATTERN.search(message)
    if not match:
        raise TestTagError(_MISSING_TAG_MESSAGE)

    value = match.group(1).strip()
    if not value:
        raise TestTagError("BAZEL_TEST_INVOCATIONS= tag is empty")

    if value.startswith(_NONE_PREFIX):
        explanation = value.removeprefix(_NONE_PREFIX).strip()
        if not explanation:
            raise TestTagError(
                "BAZEL_TEST_INVOCATIONS=none: requires an explanation (e.g., BAZEL_TEST_INVOCATIONS=none: documentation-only change)"
            )
        return NoTests(explanation)

    items = [_parse_invocation_ref(raw.strip()) for raw in value.split(",")]
    return Invocations(items)


@tenacity.retry(
    retry=tenacity.retry_if_exception_type(_RetriableHTTPError),
    stop=tenacity.stop_after_attempt(5),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=16),
    before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _fetch_invocation_attempt(inv_id: uuid.UUID, api_key: str) -> invocation_pb2.Invocation:
    """Single attempt to fetch an invocation; raises _RetriableHTTPError on transient HTTP errors."""
    try:
        resp = httpx.post(
            _BUILDBUDDY_API_URL,
            json={"lookup": {"invocationId": str(inv_id)}},
            headers={"x-buildbuddy-api-key": api_key},
            timeout=5.0,
        )
    except httpx.HTTPError as e:
        raise TestTagError(f"Failed to verify invocation {inv_id}: {e}") from e
    if resp.status_code in _RETRIABLE_STATUS_CODES:
        raise _RetriableHTTPError(resp.status_code)
    if resp.status_code != 200:
        raise TestTagError(f"BuildBuddy API returned HTTP {resp.status_code} for invocation {inv_id}")
    resp_proto = json_format.Parse(resp.text, invocation_pb2.GetInvocationResponse(), ignore_unknown_fields=True)
    if not resp_proto.invocation:
        raise TestTagError(f"BuildBuddy invocation {inv_id} not found")
    return resp_proto.invocation[0]


def _get_invocation(inv_id: uuid.UUID, api_key: str) -> invocation_pb2.Invocation:
    """Fetch a single invocation from BuildBuddy, retrying on transient errors."""
    try:
        return _fetch_invocation_attempt(inv_id, api_key)
    except _RetriableHTTPError as e:
        raise TestTagError(f"BuildBuddy API unavailable for invocation {inv_id} after retries: {e}") from e


def _get_child_invocation_ids(inv: invocation_pb2.Invocation) -> list[str]:
    """Extract child invocation IDs from a workflow/runner invocation's build events."""
    children: list[str] = []
    for event in inv.event:
        for child in event.build_event.children:
            cid = child.child_invocation_completed.invocation_id
            if cid:
                children.append(cid)
    return children


def verify_invocations_on_buildbuddy(ids: list[uuid.UUID]) -> None:
    """Query BuildBuddy to check invocation IDs exist and are test runs.

    If an invocation is a wrapper (e.g. ``bbr`` runner with command ``remote test``),
    automatically resolves to its child invocation and verifies that instead.
    """
    api_key = os.environ.get("BUILDBUDDY_API_KEY")
    if not api_key:
        return

    for inv_id in ids:
        inv = _get_invocation(inv_id, api_key)
        command = inv.command
        if command == "test":
            continue

        # Not a direct test invocation — try resolving child invocations (bbr wrapper pattern).
        children = _get_child_invocation_ids(inv)
        if not children:
            raise TestTagError(f"BuildBuddy invocation {inv_id} is a '{command}' invocation, not 'test'")

        # Verify at least one child is a test invocation.
        for child_id_str in children:
            child_inv = _get_invocation(uuid.UUID(child_id_str), api_key)
            child_command = child_inv.command
            if child_command == "test":
                break
        else:
            raise TestTagError(
                f"BuildBuddy invocation {inv_id} is a '{command}' wrapper, "
                f"but none of its {len(children)} child invocation(s) are 'test'"
            )


def check_commit_message(message: str) -> None:
    """Check a commit message for a valid BAZEL_TEST_INVOCATIONS= tag. Raises TestTagError on failure."""
    if is_exempt(message):
        return

    tag = parse_test_tag(message)
    match tag:
        case Invocations(items=items):
            bb_ids = [inv.id for inv in items if isinstance(inv, BuildBuddyInvocation)]
            if bb_ids:
                verify_invocations_on_buildbuddy(bb_ids)
        case NoTests():
            pass
