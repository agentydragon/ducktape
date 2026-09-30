"""Read-only probe of the live routes against the real API, for finding out why one answers as it does
(what it found: docs/api.md § Session watch).

Never refreshes the credential: a refresh rotates the refresh token, which the running sync owns. It reads the access
token from the file and stops if that has lapsed. Nothing secret is printed, and frame data only by its JSON keys.
"""

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice

import httpx
from httpx_sse import EventSource

from devinfra.claude.session_export.api import CCR_BETA, FIRST_PARTY_API_URL, USER_AGENT, WATCH_CLIENT_PLATFORM
from devinfra.claude.session_export.models import cse_id
from devinfra.claude.session_export.oauth import CredentialStore

# What the web client sends on its CCR calls and on the watch, beyond what the sync sends on its list and events calls.
CLIENT_FEATURE = {"anthropic-client-feature": "ccr"}
CLIENT_PLATFORM = {"anthropic-client-platform": WATCH_CLIENT_PLATFORM}
_MAX_FRAMES_SHOWN = 5
_BODY_CHARS = 200


@dataclass(frozen=True)
class Variant:
    label: str
    path: str = "/v1/code/sessions/watch"
    base_url: str = FIRST_PARTY_API_URL
    headers: dict[str, str] = field(default_factory=dict)
    beta: bool = True
    resume_token: bool = True


VARIANTS = (
    Variant("watch as the sync sends it"),
    Variant("watch + anthropic-client-feature", headers=CLIENT_FEATURE),
    Variant("watch + anthropic-client-platform", headers=CLIENT_PLATFORM),
    Variant("watch + both headers", headers=CLIENT_FEATURE | CLIENT_PLATFORM),
    Variant("watch with no resume_token (a route that exists answers 400)", resume_token=False),
    Variant("watch with no anthropic-beta", beta=False),
    Variant("watch under /v1/sessions", path="/v1/sessions/watch"),
    Variant("watch on claude.ai, both headers", base_url="https://claude.ai", headers=CLIENT_FEATURE | CLIENT_PLATFORM),
)


def _frame_shape(event: str, frame_id: str, data: str) -> str:
    try:
        document = json.loads(data)
    except ValueError:
        shape = f"{len(data)} bytes, not JSON" if data else "no data"
    else:
        shape = f"keys {sorted(document)}" if isinstance(document, dict) else "JSON, not an object"
    return f"[{event}{f' id={frame_id}' if frame_id else ''}: {shape}]"


async def _summarize_frames(response: httpx.Response, seconds: float) -> str:
    shapes: deque[str] = deque()  # appended as they arrive: a comprehension would lose them when the window closes
    with contextlib.suppress(TimeoutError):  # the window is the point: what arrived while it was open is the result
        async with asyncio.timeout(seconds):
            async for frame in EventSource(response).aiter_sse():
                shapes.append(_frame_shape(frame.event, frame.id, frame.data))
    return f"{len(shapes)} frame(s) in {seconds:g}s {' '.join(islice(shapes, _MAX_FRAMES_SHOWN))}"


async def _try(
    client: httpx.AsyncClient,
    label: str,
    url: str,
    *,
    params: dict[str, str],
    headers: dict[str, str],
    listen_seconds: float,
    out: Callable[[str], None],
) -> None:
    try:
        async with client.stream("GET", url, params=params, headers=headers) as response:
            kind = response.headers.get("content-type", "")
            if response.is_success and "text/event-stream" in kind:
                result = await _summarize_frames(response, listen_seconds)
            else:
                result = f"{kind}: {(await response.aread())[:_BODY_CHARS].decode(errors='replace')!r}"
            out(f"{response.status_code} {label}: {result}")
    except httpx.HTTPError as failure:
        out(f"ERR {label}: {type(failure).__name__}: {failure}")


async def probe(
    store: CredentialStore,
    *,
    session_id: str | None,
    listen_seconds: float,
    out: Callable[[str], None] = print,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Tries the session watch under each of `VARIANTS`, and, for `session_id`, listens to its event stream."""
    credential = store.load()
    if credential.expires_at <= datetime.now(UTC):
        raise ValueError("the access token has lapsed; the running sync refreshes it, so run this again in a moment")
    base = {
        "authorization": f"Bearer {credential.access_token.get_secret_value()}",
        "x-organization-uuid": credential.organization_uuid,
        "anthropic-version": "2023-06-01",
        "user-agent": USER_AGENT,
    }
    beta = {"anthropic-beta": CCR_BETA}
    async with httpx.AsyncClient(
        base_url=FIRST_PARTY_API_URL, headers=base, transport=transport, timeout=httpx.Timeout(15, read=listen_seconds)
    ) as client:
        listed = await client.get("/v1/code/sessions", params={"limit": 1}, headers=beta)
        out(f"{listed.status_code} list of one session")
        token = listed.json().get("resume_token") if listed.is_success else None
        if token is None:
            out("no resume_token from the list route: the watch variants that send one cannot run")
        for variant in VARIANTS:
            if variant.resume_token and token is None:
                continue
            await _try(
                client,
                variant.label,
                variant.base_url + variant.path,
                params={"exclude_tags": "-"} | ({"resume_token": token} if variant.resume_token and token else {}),
                headers=variant.headers | (beta if variant.beta else {}),
                listen_seconds=listen_seconds,
                out=out,
            )
        if session_id:
            newest = await client.get(
                f"/v1/code/sessions/{session_id}/events", params={"limit": 1, "sort_order": "desc"}, headers=beta
            )
            events = newest.json()["data"] if newest.is_success else []
            after = int(events[0]["sequence_num"]) if events else 0
            await _try(
                client,
                f"event stream of {session_id} from sequence_num {after}",
                f"{FIRST_PARTY_API_URL}/v1/code/sessions/{cse_id(session_id)}/events/stream",
                params={"from_sequence_num": str(after)} if after else {},
                headers=beta | ({"last-event-id": str(after)} if after else {}),
                listen_seconds=listen_seconds,
                out=out,
            )
