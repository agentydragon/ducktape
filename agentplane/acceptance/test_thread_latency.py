"""How fast the deployment answers a one-word turn, and how fast a browser can open that thread.

**The turn.** A session at low reasoning effort, on the model the deployment offers its harness, is
asked to say PING. Opening a session starts the harness and completes its handshake before the app
answers, so the turn runs on a started harness. Timed on the test's clock: from the app answering
the input as admitted (runner admission plus archival, `Turn.admitted`) to the turn's
`TurnCompleted` arriving on the thread's event stream. The harness reports the same turn on its own
clock in the frame that ends it: Claude's `result` `duration_ms`, Codex's `turn/completed`
`turn.durationMs`. That span holds the model call and the proxies on its path. The ceiling is on the
difference, the runner's and the app's share, since the model's leg alone varies by seconds.

**The open.** The reads `thread_store.tsx` makes before it can show that answer, through the app's
sync proxy (`agentplane/app/electric.py`), each stage timed from sending its first request to
reading its last body:

- `scope`: `GET /threads/{id}/sync/scope`, the projection epoch every shape request names.
- `entity_shape`: the thread's entity shape, opened from now; Electric creates it here, or finds it.
- `tail`: the tail, view-state and pending-command subsets of that shape, sent together as the
  browser sends them before it shows the thread.
- `body_shape`: the text field's chunk shape, opened from now.
- `body`: one subset of it naming the answer's `(owner_id, generation)`.

Electric's browser client may fold a shape's creation into its first subset; this test opens each
shape on its own, so creating a shape and reading from it land in different stages. The thread is
opened twice: cold, when nothing has opened its shapes yet, and warm, straight after, when both
exist. Every request crosses the network from the test's host, and the cold `tail` also carries the
connections its concurrent subsets open.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import httpx
import pytest_bazel
from more_itertools import one
from pydantic import BaseModel, Field

from agentplane.acceptance.agent import Agent
from agentplane.app.agent_runtime.view.fold import PayloadField
from agentplane.app.agent_runtime.view.views import EntityKind, ThreadItemState, ThreadPayloadReference
from agentplane.app.client import REQUEST_SECONDS, Client
from agentplane.app.electric import SUBSET_ROW_LIMIT, SubsetRequest, ThreadScopeResponse
from agentplane.app.inventory import SandboxView
from agentplane.app.presets import Harness
from agentplane.native.claude import wire as claude_wire  # Both harnesses name their frame module `wire`.
from agentplane.native.codex import wire as codex_wire
from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2
from util.testing.undeclared_outputs import undeclared_outputs_dir

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

Sandboxes = Callable[..., Awaitable[SandboxView]]
# An Electric row as the proxy relays it: every value its column's text form, or null.
Row = dict[str, Any]

PING = "Say exactly the word PING and nothing else. Do not use any tool."
# The target for what agentplane adds to the harness's own turn: the runner's journal, the app's
# ingest and stream, and the trip to the test's host. A small fixed cost, whatever the turn's length.
ADDED_SECONDS = 0.5


class Stage(StrEnum):
    SCOPE = "scope"
    ENTITY_SHAPE = "entity_shape"
    TAIL = "tail"
    BODY_SHAPE = "body_shape"
    BODY = "body"


# Regression gates, not targets: each sits about an order of magnitude above what its stage should
# cost, so a breach names a stage that broke rather than a slow network. A developer host has seen
# the app's first byte in about 0.1 s, so a second is a lookup gone wrong; the subsets are bounded,
# indexed reads Electric runs in PostgreSQL. Creating a shape writes to Electric's network volume,
# and the slow opens reported on staging spent ~20 s per request upstream of the app; five seconds
# catches a return to that. A warm shape is a lookup.
COLD_CEILINGS = {Stage.SCOPE: 1.0, Stage.ENTITY_SHAPE: 5.0, Stage.TAIL: 2.0, Stage.BODY_SHAPE: 5.0, Stage.BODY: 2.0}
WARM_CEILINGS = COLD_CEILINGS | {Stage.ENTITY_SHAPE: 1.0, Stage.BODY_SHAPE: 1.0}

# The subsets `EpochWindow` in thread_store.tsx loads before it shows a thread, in the forms the
# proxy admits; the tail is its `PAGE`.
NEWEST_FIRST = "entity_index DESC"
TAIL = SubsetRequest(order_by=NEWEST_FIRST, limit=30)
VIEW_STATE = SubsetRequest(where=f"entity_kind = '{EntityKind.VIEW_STATE}'", order_by=NEWEST_FIRST, limit=1)
PENDING_COMMANDS = SubsetRequest(
    where=f"entity_kind = '{EntityKind.COMMAND}' AND pending = true", order_by=NEWEST_FIRST, limit=SUBSET_ROW_LIMIT
)


class Timings(BaseModel):
    harness: Harness
    model: str
    thread_id: UUID
    turn_seconds: float = Field(description="From the app admitting the PING input to its TurnCompleted arriving.")
    harness_seconds: float = Field(description="The same turn as the harness reports it on its own clock.")
    cold: dict[Stage, float] = Field(description="Each stage of the thread's first open.")
    warm: dict[Stage, float] = Field(description="Each stage of the same open, repeated at once.")


@dataclass(frozen=True)
class ThreadOpen:
    seconds: dict[Stage, float]
    answers: list[str]


@dataclass(frozen=True)
class _Shape:
    """A shape the proxy opened from now, and the handle and offset its subsets name."""

    http: httpx.AsyncClient
    path: str
    params: dict[str, str]

    async def subset(self, request: SubsetRequest) -> list[Row]:
        response = await self.http.post(self.path, params=self.params, json=request.model_dump(exclude_none=True))
        assert response.status_code == httpx.codes.OK, f"{request}: {response.status_code} {response.text}"
        return [message["value"] for message in response.json()["data"] if "value" in message]


async def _open_shape(http: httpx.AsyncClient, path: str, projection_epoch: str) -> _Shape:
    response = await http.get(path, params={"projection_epoch": projection_epoch, "offset": "now"})
    response.raise_for_status()
    handle, offset = response.headers["electric-handle"], response.headers["electric-offset"]
    return _Shape(http, path, {"projection_epoch": projection_epoch, "handle": handle, "offset": offset})


@contextmanager
def _timed(seconds: dict[Stage, float], stage: Stage) -> Iterator[None]:
    started = time.monotonic()
    yield
    seconds[stage] = time.monotonic() - started


def _harness_seconds(harness: protocol_pb2.Harness, native: list[str]) -> float:
    """The turn's duration as the harness reports it in the frame that ends the turn."""
    frames = [json.loads(line) for line in native]
    milliseconds: int | None
    match harness:
        case protocol_pb2.HARNESS_CLAUDE:
            milliseconds = one(
                frame.duration_ms
                for frame in map(claude_wire.parse_frame, frames)
                if isinstance(frame, claude_wire.ResultFrame)
            )
        case protocol_pb2.HARNESS_CODEX:
            milliseconds = one(
                frame.params.turn.duration_ms
                for frame in map(codex_wire.parse_frame, frames)
                if isinstance(frame, codex_wire.TurnCompleted)
            )
        case _:
            raise AssertionError(f"no reported turn duration for {harness=}")
    assert milliseconds is not None, "the harness completed the turn without reporting its duration"
    return milliseconds / 1000


def _answers(tail: list[Row]) -> list[ThreadPayloadReference]:
    """The assistant-text bodies the tail names: what the browser mounts to show the answer."""
    return [
        ThreadPayloadReference.model_validate_json(row["text_ref"])
        for row in tail
        if row["entity_kind"] == EntityKind.ITEM
        and row["text_ref"] is not None
        and ThreadItemState.model_validate_json(row["state"]).kind == event_pb2.ITEM_KIND_ASSISTANT_TEXT
    ]


def _bodies(references: list[ThreadPayloadReference]) -> SubsetRequest:
    """One read naming every body, as `bodySubset` in thread_store.tsx forms it."""
    return SubsetRequest(
        where=" OR ".join(
            f"(owner_id = ${2 * n - 1} AND generation = ${2 * n})" for n in range(1, len(references) + 1)
        ),
        params={
            key: value
            for n, reference in enumerate(references, 1)
            for key, value in ((str(2 * n - 1), reference.owner_id), (str(2 * n), reference.generation))
        },
    )


def _body(chunks: list[Row], reference: ThreadPayloadReference) -> str:
    """The body as far as its reference spans it, which is what the browser renders."""
    texts = {
        int(chunk["chunk_index"]): chunk["text"]
        for chunk in chunks
        if chunk["owner_id"] == reference.owner_id and chunk["generation"] == reference.generation
    }
    return "".join(texts[index] for index in range(int(reference.chunk_count)))


async def _open_thread(http: httpx.AsyncClient, thread_id: UUID) -> ThreadOpen:
    seconds: dict[Stage, float] = {}
    sync = f"/threads/{thread_id}/sync"
    with _timed(seconds, Stage.SCOPE):
        response = await http.get(f"{sync}/scope")
        response.raise_for_status()
        scope = ThreadScopeResponse.model_validate(response.json())
    with _timed(seconds, Stage.ENTITY_SHAPE):
        entities = await _open_shape(http, f"{sync}/entities", scope.projection_epoch)
    with _timed(seconds, Stage.TAIL):
        tail, _, _ = await asyncio.gather(
            entities.subset(TAIL), entities.subset(VIEW_STATE), entities.subset(PENDING_COMMANDS)
        )
    answers = _answers(tail)
    assert answers, f"the tail names no assistant text: {tail}"
    with _timed(seconds, Stage.BODY_SHAPE):
        chunks = await _open_shape(http, f"{sync}/chunks/{PayloadField.TEXT}", scope.projection_epoch)
    with _timed(seconds, Stage.BODY):
        rows = await chunks.subset(_bodies(answers))
    return ThreadOpen(seconds=seconds, answers=[_body(rows, reference) for reference in answers])


async def test_a_one_word_turn_answers_and_its_thread_opens_in_time(
    client: Client, base_url: str, token: str, sandbox: Sandboxes, harness: protocol_pb2.Harness, model: str
) -> None:
    view = await sandbox(f"accept-latency-{harness}")
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=model)
    turn = await agent.run(PING)
    turn_seconds = time.monotonic() - turn.admitted
    assert "PING" in turn.answer, turn.transcript
    harness_seconds = _harness_seconds(harness, turn.native)

    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_SECONDS
    ) as http:
        cold = await _open_thread(http, agent.thread_id)
        warm = await _open_thread(http, agent.thread_id)

    timings = Timings(
        harness=Harness(protocol_pb2.Harness.Name(harness)),
        model=model,
        thread_id=agent.thread_id,
        turn_seconds=turn_seconds,
        harness_seconds=harness_seconds,
        cold=cold.seconds,
        warm=warm.seconds,
    )
    (undeclared_outputs_dir() / f"thread_latency-{timings.harness}.json").write_text(timings.model_dump_json(indent=2))

    # Each open read the answer back, so no stage was fast by returning nothing.
    for opened in (cold, warm):
        assert any("PING" in answer for answer in opened.answers), opened.answers
    added = turn_seconds - harness_seconds
    breaches = (
        [f"turn {turn_seconds:.3f}s, harness {harness_seconds:.3f}s: added {added:.3f}s, ceiling {ADDED_SECONDS}s"]
        if added >= ADDED_SECONDS
        else []
    )
    breaches += [
        f"{label} {stage} {seconds:.3f}s, ceiling {ceilings[stage]}s"
        for label, opened, ceilings in (("cold", cold, COLD_CEILINGS), ("warm", warm, WARM_CEILINGS))
        for stage, seconds in opened.seconds.items()
        if seconds >= ceilings[stage]
    ]
    assert not breaches, f"{'; '.join(breaches)}\n{timings.model_dump_json()}"


if __name__ == "__main__":
    pytest_bazel.main()
