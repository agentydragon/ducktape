"""Payload bodies, stored insert-only: a manifest row per revision and chunk rows per generation,
so a body once written never changes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.app import thread_fold
from agentplane.app.trajectory.models import ThreadPayloadChunk, ThreadPayloadManifest


@dataclass
class _PayloadPlan:
    reference: thread_fold.PayloadRef
    prefix_chunks: int
    prefix_bytes: int
    fragments: list[str]
    replaced: bool


async def write_payloads(session: AsyncSession, thread_id: UUID, writes: Sequence[thread_fold.PayloadWrite]) -> None:
    plans_by_reference: dict[thread_fold.PayloadRef, _PayloadPlan] = {}
    final_plans: dict[tuple[int, str, thread_fold.PayloadField], _PayloadPlan] = {}
    for write in writes:
        if isinstance(write, thread_fold.ReplacePayload):
            plan = _PayloadPlan(write.reference, 0, 0, [write.text], True)
        else:
            prior = plans_by_reference.get(write.base) if write.base is not None else None
            if prior is None:
                if write.base is None:
                    prior_chunks = 0
                    prior_bytes = 0
                else:
                    manifest = await session.get(ThreadPayloadManifest, _payload_manifest_key(thread_id, write.base))
                    if manifest is None:
                        raise ValueError("append references a missing payload manifest")
                    prior_chunks = manifest.chunk_count
                    prior_bytes = manifest.content_bytes
                plan = _PayloadPlan(write.reference, prior_chunks, prior_bytes, [write.text], False)
            else:
                prior.fragments.append(write.text)
                plan = _PayloadPlan(
                    write.reference, prior.prefix_chunks, prior.prefix_bytes, prior.fragments, prior.replaced
                )
        plans_by_reference[plan.reference] = plan
        final_plans[(plan.reference.owner_cursor, plan.reference.owner_id, plan.reference.field)] = plan
    for plan in final_plans.values():
        text = "".join(plan.fragments)
        text_bytes = len(text.encode())
        chunk_count = plan.prefix_chunks if not text else plan.prefix_chunks + 1
        await session.execute(
            insert(ThreadPayloadManifest).values(
                thread_id=thread_id,
                projection_epoch=plan.reference.projection_epoch,
                owner_cursor=plan.reference.owner_cursor,
                owner_id=plan.reference.owner_id,
                field=plan.reference.field,
                generation=plan.reference.generation,
                revision_cursor=plan.reference.revision_cursor,
                chunk_count=chunk_count,
                content_bytes=text_bytes if plan.replaced else plan.prefix_bytes + text_bytes,
            )
        )
        if chunk_count > plan.prefix_chunks:
            session.add(
                ThreadPayloadChunk(
                    thread_id=thread_id,
                    projection_epoch=plan.reference.projection_epoch,
                    owner_cursor=plan.reference.owner_cursor,
                    owner_id=plan.reference.owner_id,
                    field=plan.reference.field,
                    generation=plan.reference.generation,
                    chunk_index=plan.prefix_chunks,
                    text=text,
                )
            )


def _payload_manifest_key(
    thread_id: UUID, reference: thread_fold.PayloadRef
) -> tuple[UUID, str, int, str, str, int, int]:
    return (
        thread_id,
        reference.projection_epoch,
        reference.owner_cursor,
        reference.owner_id,
        reference.field,
        reference.generation,
        reference.revision_cursor,
    )
