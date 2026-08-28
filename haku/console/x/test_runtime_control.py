"""The operator's admission switch: closed refuses new prompts, in the accepting transaction."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel

from haku.console.chat_models import SPA_ORIGIN, PromptRejection
from haku.console.x.session_store import PromptRefusedError, SessionStore
from haku.runtime.x.bridge.neutral_operations import GENERATION


async def test_the_switch_lands_open_with_the_cut_generation(session_store: SessionStore) -> None:
    """The migration created the row open, naming this build's generation — the pair the
    operator's dashboard reads before resuming use, pinned against the code constant a runner
    presents so the two cannot drift."""
    assert await session_store.active_generation() == GENERATION
    assert await session_store.admission_open()


async def test_closed_admission_refuses_submission_and_reopening_admits(
    session_store: SessionStore, operator_id: UUID
) -> None:
    view, _ = await session_store.create(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    await session_store.set_admission_open(open_admission=False)

    with pytest.raises(PromptRefusedError) as refusal:
        await session_store.submit_prompt(operator_id, conversation_id, "hello?", SPA_ORIGIN)
    assert refusal.value.reason is PromptRejection.ADMISSION_CLOSED

    await session_store.set_admission_open(open_admission=True)
    await session_store.submit_prompt(operator_id, conversation_id, "hello again", SPA_ORIGIN)


if __name__ == "__main__":
    pytest_bazel.main()
