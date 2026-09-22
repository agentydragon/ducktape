"""What opening a conversation costs against the deployed system, by the reads the browser makes.

The conversations page was reported taking tens of seconds to show text that had already arrived.
That was never visible to a unit suite: the cost was Electric shape creation on the deployed
storage, which no fixture reproduces and a HAR reports only as time spent upstream. So the check
belongs here, on the deployment, and it measures the sequence the browser actually performs rather
than a proxy for it -- resolve the interest, take the entity snapshot, then read every completed
body it would render.

The ceilings below are regression gates, not targets. A one-turn conversation should open in well
under a second; the gates are loose enough to pass while `agentplane/plans/conversation_sync_latency.md`
still has W1 open, and tight enough that the reported failure could not have passed them. Each stage
is timed separately so a breach names the stage instead of the total.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
import pytest_bazel

from agentplane.acceptance.agent import Agent
from agentplane.app.client import Client
from agentplane.app.inventory import SandboxView
from agentplane.runner import protocol_pb2

# `protocol_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

# A turn that asks for one word at low reasoning effort. The model is the variable here, not
# Agentplane, so this gate only catches a turn that stopped being a turn -- a harness waiting on
# something, a runner not forwarding -- rather than ordinary model variance.
PING_SECONDS = 5.0
# The whole browser-shaped open of that conversation: interest, entity snapshot, every body it
# renders. The reported failure was ~20s with the bodies alone costing ~30 Electric shapes.
OPEN_SECONDS = 5.0
# Reading the completed bodies, which no longer involves Electric at all. Nothing here is more than
# two indexed queries and a concatenation, so a breach means they went back through a shape.
BODIES_SECONDS = 2.0

PING = "Reply with exactly the word PING and nothing else."


@dataclass
class ConversationOpen:
    """Wall time of each stage a browser passes through before it can paint the conversation."""

    interest_seconds: float = 0.0
    entities_seconds: float = 0.0
    bodies_seconds: float = 0.0
    bodies: list[str] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return self.interest_seconds + self.entities_seconds + self.bodies_seconds

    def __str__(self) -> str:
        return (
            f"interest={self.interest_seconds:.3f}s entities={self.entities_seconds:.3f}s "
            f"bodies={self.bodies_seconds:.3f}s ({len(self.bodies)} read) total={self.total_seconds:.3f}s"
        )


async def _open_conversation(http: httpx.AsyncClient, thread_id: str) -> ConversationOpen:
    """The reads `conversation_store.tsx` makes to paint a thread, in the order it makes them."""
    timings = ConversationOpen()
    sync = f"/threads/{thread_id}/sync"

    started = time.monotonic()
    interest = await http.get(f"{sync}/interest")
    interest.raise_for_status()
    selection = interest.json()
    timings.interest_seconds = time.monotonic() - started

    entity_params = {key: selection[key] for key in ("source_id", "projection_epoch", "anchor_cursor", "tail_from")}
    started = time.monotonic()
    # The TanStack collection's own handshake: a current offset, then the whole fixed interest.
    start = await http.get(f"{sync}/entities", params=entity_params | {"offset": "now"})
    start.raise_for_status()
    snapshot = await http.get(
        f"{sync}/entities",
        params=entity_params
        | {
            "offset": start.headers["electric-offset"],
            "handle": start.headers["electric-handle"],
            "subset__where": "true = true",
        },
    )
    snapshot.raise_for_status()
    timings.entities_seconds = time.monotonic() - started

    rows = [message["value"] for message in snapshot.json()["data"] if "value" in message]
    references = [
        reference
        for row in rows
        for key in ("text_ref", "input_ref", "arguments_ref", "output_ref")
        if (reference := row.get(key)) is not None
    ]
    started = time.monotonic()
    for reference in references:
        body = await http.get(
            f"/threads/{thread_id}/conversation/payload",
            params={
                "source_id": reference["source_id"],
                "projection_epoch": reference["projection_epoch"],
                "owner_cursor": reference["owner_cursor"],
                "owner_id": reference["owner_item_id"],
                "field": reference["field"],
                "generation": reference["generation"],
                "revision_cursor": reference["revision_cursor"],
            },
        )
        body.raise_for_status()
        answered = body.json()
        if answered["availability"] == "present":
            timings.bodies.append(answered["body"])
    timings.bodies_seconds = time.monotonic() - started
    return timings


async def test_a_one_turn_conversation_opens_interactively(
    client: Client,
    base_url: str,
    token: str,
    sandbox: Callable[..., Awaitable[SandboxView]],
    harness: protocol_pb2.Harness,
    model: str,
) -> None:
    box = await sandbox("conversation-latency")
    agent = await Agent.open(client, sandbox=box.name, harness=harness, model=model)

    # The harness is up by now, so this times a turn and not a container start.
    started = time.monotonic()
    turn = await agent.run(PING)
    ping_seconds = time.monotonic() - started
    assert "PING" in "".join(turn.text), turn.transcript

    async with httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=60) as http:
        cold = await _open_conversation(http, str(agent.thread_id))
        # A second open of the same conversation reuses whatever the first one created. Reporting
        # both is what distinguishes a slow query from a shape that had to be built.
        warm = await _open_conversation(http, str(agent.thread_id))

    # Both halves of the exchange come back through the payload route, not just the metadata that
    # names them: the confirmed input, and an assistant body that is not simply that input echoed.
    assert PING in cold.bodies, cold.bodies
    assert any("PING" in body for body in cold.bodies if body != PING), cold.bodies
    assert ping_seconds < PING_SECONDS, f"one-word turn took {ping_seconds:.3f}s"
    assert cold.bodies_seconds < BODIES_SECONDS, f"completed bodies: cold {cold} warm {warm}"
    assert cold.total_seconds < OPEN_SECONDS, f"cold open {cold}; warm open {warm}"


if __name__ == "__main__":
    pytest_bazel.main()
