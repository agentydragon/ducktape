"""Finite Electric workload probe; response bodies and kernel memory counters are test artifacts.

The reader holds six older items while new completed items and an active tail grow outside that
window. This measures the deployed shape protocol directly, without browser rendering or the app
proxy; it cannot establish a long-running memory bound or compressed browser wire traffic.
"""

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest_bazel
from sqlalchemy import select

from agentplane.app.testing.replication_source import ReplicationSource
from agentplane.history_service.fold.models import FOLD_EPOCH, ThreadEntity
from agentplane.history_service.testing.electric_service import electric_service
from agentplane.history_service.testing.history import History
from agentplane.proto import event_pb2
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

Row = dict[str, Any]


@dataclass(frozen=True)
class _Tail:
    new_ids: set[str]
    active: str
    revision: int

    def entities_caught_up(self, changes: list[Row]) -> bool:
        return self.new_ids <= {change["value"]["entity_id"] for change in changes} and any(
            change["value"]["entity_id"] == self.active and int(change["value"]["revision_cursor"]) == self.revision
            for change in changes
        )

    def chunks_caught_up(self, changes: list[Row]) -> bool:
        return self.new_ids <= {change["value"]["owner_id"] for change in changes} and any(
            change["value"]["owner_id"] == self.active and int(change["value"]["chunk_index"]) == 7
            for change in changes
        )


@dataclass
class _Shape:
    client: httpx.AsyncClient
    params: dict[str, str]
    name: str
    selected: set[str]
    handle: str = field(init=False)
    offset: str = "now"
    responses: list[dict[str, Any]] = field(default_factory=list)

    def save(self, response: httpx.Response, stage: str) -> None:
        path = f"{self.name}-{len(self.responses):03}-{stage}.json"
        (undeclared_outputs_dir() / path).write_bytes(response.content)
        self.responses.append(
            {
                "stage": stage,
                "body": path,
                "response_body_bytes": len(response.content),
                "content_encoding": response.headers.get("content-encoding"),
                "offset": response.headers["electric-offset"],
            }
        )

    async def open(self) -> None:
        response = await self.client.get("/v1/shape", params=self.params | {"offset": "now"})
        response.raise_for_status()
        self.handle, self.offset = response.headers["electric-handle"], response.headers["electric-offset"]
        self.save(response, "open")

    async def subset(self, body: dict[str, object]) -> list[Row]:
        response = await self.client.post(
            "/v1/shape", params=self.params | {"handle": self.handle, "offset": self.offset}, json=body
        )
        response.raise_for_status()
        self.save(response, "subset")
        # A subset's offset must not advance the whole-thread live cursor.
        return [message["value"] for message in response.json()["data"] if "value" in message]

    async def follow(self, stage: str, done: Callable[[list[Row]], bool]) -> dict[str, int]:
        changes: list[Row] = []
        before = len(self.responses)
        while not done(changes):
            response = await self.client.get(
                "/v1/shape", params=self.params | {"handle": self.handle, "offset": self.offset, "live": "true"}
            )
            response.raise_for_status()
            self.save(response, stage)
            self.offset = response.headers["electric-offset"]
            changes.extend(message for message in response.json() if "value" in message)
        key = "entity_id" if self.name == "entities" else "owner_id"
        held = sum(change["value"][key] in self.selected for change in changes)
        control = sum(change["value"].get("entity_kind") == "view_state" for change in changes)
        return {
            "response_body_bytes": sum(int(row["response_body_bytes"]) for row in self.responses[before:]),
            "selected_item_changes": held,
            "view_state_changes": control,
            "unselected_item_changes": len(changes) - held - control,
        }


def _item(item: str) -> list[event_pb2.Event]:
    return [
        event_pb2.Event(item_started=event_pb2.ItemStarted(item_id=item, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)),
        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id=item, text="x" * 2048)),
        event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id=item, text="x" * 2048)),
    ]


async def test_fixed_older_window_receives_busy_tail_changes(
    history_on: Callable[[str], AbstractAsyncContextManager[History]],
) -> None:
    report: dict[str, Any] = {
        "window_items": 6,
        "item_body_bytes": 2048,
        "stages": [],
        "transport": "Electric HTTP long-poll response bodies; Accept-Encoding: identity",
    }
    artifact = undeclared_outputs_dir() / "electric-window-traffic.json"
    async with electric_service() as service, history_on(service.database_url) as history:
        source = ReplicationSource()
        thread = source.session_id
        await history.open(thread)
        await history.set_attached(thread, source.attached)

        async def record(events: list[event_pb2.Event]) -> None:
            await history.record(thread, [source.append(event) for event in events])

        async def entities() -> list[ThreadEntity]:
            async with history.sessions() as session:
                return list(await session.scalars(select(ThreadEntity).where(ThreadEntity.thread_id == thread)))

        await record(
            [
                event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=1)),
                event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="probe-turn", model="test-model")),
                *(event for index in range(12) for event in _item(f"older-{index:02}")),
            ]
        )
        selected = {f"older-{index:02}" for index in range(6)}
        initial = [row for row in await entities() if row.entity_id in selected]
        assert len(initial) == 6
        report["before_shapes_memory"] = await service.memory_snapshot()
        params = {
            "where": "thread_id = $1 AND projection_epoch = $2",
            "params[1]": str(thread),
            "params[2]": FOLD_EPOCH,
            "log": "changes_only",
            "replica": "full",
        }
        async with (
            asyncio.timeout(120),
            httpx.AsyncClient(base_url=service.url, timeout=35, headers={"Accept-Encoding": "identity"}) as client,
        ):
            rows = _Shape(client, params | {"table": "thread_entity"}, "entities", selected)
            chunks = _Shape(
                client,
                params
                | {"table": "thread_payload_chunk", "where": params["where"] + " AND field = $3", "params[3]": "text"},
                "chunks",
                selected,
            )
            await asyncio.gather(rows.open(), chunks.open())
            held = await rows.subset(
                {
                    "where": "entity_index < $1",
                    "params": {"1": str(max(row.entity_index for row in initial) + 1)},
                    "order_by": "entity_index DESC",
                    "limit": 6,
                }
            )
            assert {row["entity_id"] for row in held} == selected
            references = [row["text_ref"] for row in held]
            assert all(reference is not None for reference in references)
            held_chunks = await chunks.subset(
                {
                    "where": " OR ".join(
                        f"(owner_id = ${2 * index + 1} AND generation = ${2 * index + 2})" for index in range(6)
                    ),
                    "params": {
                        str(2 * index + part + 1): str(reference[column])
                        for index, reference in enumerate(references)
                        for part, column in enumerate(("owner_id", "generation"))
                    },
                }
            )
            assert {row["owner_id"] for row in held_chunks} == selected
            report["selected_window_memory"] = await service.memory_snapshot()
            artifact.write_text(json.dumps(report, indent=2) + "\n")

            for count in (32, 128, 512):
                stage = f"grow-{count}"
                new_ids = {f"{stage}-{index:04}" for index in range(count)}
                await record([event for item in sorted(new_ids) for event in _item(item)])
                active = f"active-{stage}"
                await record(
                    [
                        event_pb2.Event(
                            item_started=event_pb2.ItemStarted(item_id=active, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                        )
                    ]
                )
                # Separate commits exercise updates to an existing offscreen entity and chunk appends.
                for _ in range(8):
                    await record([event_pb2.Event(text_delta=event_pb2.TextDelta(item_id=active, text="y" * 2048))])
                current = await entities()
                active_row = next(row for row in current if row.entity_id == active)
                tail = _Tail(new_ids, active, active_row.revision_cursor)
                entity_traffic, chunk_traffic = await asyncio.gather(
                    rows.follow(stage, tail.entities_caught_up), chunks.follow(stage, tail.chunks_caught_up)
                )
                assert entity_traffic["selected_item_changes"] == chunk_traffic["selected_item_changes"] == 0
                assert entity_traffic["unselected_item_changes"] >= count + 1
                assert chunk_traffic["unselected_item_changes"] >= count + 8
                assert {row.entity_id: row.revision_cursor for row in current if row.entity_id in selected} == {
                    row.entity_id: row.revision_cursor for row in initial
                }
                report["stages"].append(
                    {
                        "new_completed_items": count,
                        "active_tail_append_commits": 8,
                        "stored_entity_rows": len(current),
                        "entity_traffic": entity_traffic,
                        "chunk_traffic": chunk_traffic,
                        "memory": await service.memory_snapshot(),
                    }
                )
                report["responses"] = {"entities": rows.responses, "chunks": chunks.responses}
                artifact.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    pytest_bazel.main()
