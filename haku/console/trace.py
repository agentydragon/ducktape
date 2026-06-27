"""Trace tier: operator-expressed intent recorded into haku-state.

This endpoint only writes to the haku-state repo — a plain text note in ``intake/``.
It grants Haku nothing it can't already do (it owns haku-state), so this is the
**low-privilege** tier, safe to expose to agent-authored UI. The **high-privilege**
capability tier (console-only secrets, real-world side effects) is a separate router,
gated and audited; see ``haku/PLAN.md`` → _The agent-authored console_.

The trace tier is now item-agnostic: the frontend constructs whatever text it wants
(e.g. "item blah action blah") and sends it as an opaque string.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from haku.console.deps import GitStateDep
from haku.console.models import TraceRequest

router = APIRouter(prefix="/api/trace", tags=["trace"])


@router.post("")
async def trace(req: TraceRequest, git_state: GitStateDep) -> dict[str, str]:
    """Append an operator-authored note to haku-state intake and commit-push."""
    async with git_state.lock:
        await asyncio.to_thread(git_state.append_trace, req.text)
    return {"status": "ok"}
