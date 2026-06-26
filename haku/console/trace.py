"""Trace tier: operator-expressed intent recorded into haku-state.

These endpoints only write to the haku-state repo — the `clicks/` overlay Haku
reduces, and feedback notes in `intake/`. They grant Haku nothing it can't already
do (it owns haku-state), so this is the **low-privilege** tier, safe to expose to
agent-authored UI. The **high-privilege** capability tier (console-only secrets,
real-world side effects) is a separate router, gated and audited; see
`haku/PLAN.md` → _The agent-authored console_.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request

from haku.console.git_state import GitState
from haku.console.models import FeedbackRequest


def _git_state(request: Request) -> GitState:
    # app.state is Starlette's untyped (Any) container; create_app puts the GitState there.
    return cast(GitState, request.app.state.git_state)


GitStateDep = Annotated[GitState, Depends(_git_state)]

router = APIRouter(prefix="/api/trace", tags=["trace"])


# Recording the overlay is an idempotent set (the marker either exists or not), so
# the verb is PUT, not POST. Haku reduces the clicks/ overlay on its next run.
@router.put("/items/{item_id}/actions/{action_id}")
async def set_click(item_id: str, action_id: str, git_state: GitStateDep) -> dict[str, str]:
    async with git_state.lock:
        await asyncio.to_thread(git_state.set_click, item_id, action_id)
    return {"status": "clicked"}


@router.delete("/items/{item_id}/actions/{action_id}")
async def clear_click(item_id: str, action_id: str, git_state: GitStateDep) -> dict[str, str]:
    async with git_state.lock:
        await asyncio.to_thread(git_state.clear_click, item_id, action_id)
    return {"status": "cleared"}


@router.post("/feedback")
async def feedback(req: FeedbackRequest, git_state: GitStateDep) -> dict[str, str]:
    async with git_state.lock:
        await asyncio.to_thread(git_state.write_feedback, req.text, req.item_id)
    return {"status": "ok"}
