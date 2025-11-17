from __future__ import annotations

from adgn.agent.agent import MiniCodex
from adgn.agent.loop_control import Abort, Auto, Continue
from adgn.agent.reducer import BaseHandler


class SyntheticOnceHandler(BaseHandler):
    """Emits one SyntheticAction with precomputed SDK outputs, then stops."""

    def __init__(self, outputs) -> None:
        self._done = False
        self._outputs = list(outputs)

    def on_before_sample(self):  # type: ignore[override]
        if self._done:
            return Abort()
        self._done = True
        return Continue(Auto(), inserts_input=tuple(self._outputs), skip_sampling=True)


async def test_mini_codex_handles_synthetic_action_without_api_calls(
    fake_openai_client_factory, responses_factory, make_pg_compositor, approval_policy_reader_allow_all
) -> None:
    client = fake_openai_client_factory([responses_factory.make_assistant_message("should_not_be_used")])
    # Build a compositor with no extra servers (just policy gateway)
    async with make_pg_compositor({"approval_policy": approval_policy_reader_allow_all}) as (mcp_client, _comp):
        resp = responses_factory.make_assistant_message("hello")
        agent = await MiniCodex.create(
            model=responses_factory.model,
            mcp_client=mcp_client,
            system="You are a code agent.",
            client=client,
            handlers=[SyntheticOnceHandler(resp.output)],
        )
        res = await agent.run("hi")
        assert res.text.strip() == "hello"
        # MiniCodex uses the protocol method `.responses_create` — ensure we made no API calls.
        assert getattr(client, "calls", 0) == 0
