from pathlib import Path

import pytest

from adgn.mcp._shared.naming import build_mcp_function
from adgn.openai_utils.model import FunctionToolParam, ResponsesRequest
from adgn.props.critic import CriticSubmitPayload
import adgn.props.prompt_eval.server
from adgn.props.prompt_eval.server import PromptEvalArgs, PromptEvalOutput, build_server
from tests.fixtures.responses import ResponsesFactory  # single factory for adapter responses

from .support.openai_mock import LIVE  # sentinel for live client


# Behavior (mock): our Pydantic request; return our Pydantic ResponsesResult
async def _behavior_ok(req):
    assert isinstance(req, ResponsesRequest), f"unexpected request type: {type(req)!r}"
    responses_factory = ResponsesFactory("gpt-5-nano")

    # If grader tools are offered, simulate a function_call to submit_result
    tools = req.tools
    if isinstance(tools, list):
        names: list[str] = []
        for t in tools:
            if isinstance(t, FunctionToolParam):
                names.append(t.name)
        # Inspect offered tool names for debugging
        print("[test] offered tools:", names)
        target = build_mcp_function("grader_submit", "submit_result")
        if target in names:
            args = {
                "result": {
                    "true_positive_ids": [],
                    "false_positive_ids": [],
                    "unknown_critique_ids": [],
                    "precision": 1.0,
                    "recall": 1.0,
                    "message_md": "ok",
                }
            }
            return responses_factory.make(responses_factory.tool_call(target, args))

    # Otherwise: critic path → simple assistant text
    inp = req.input
    text = "ok"
    if isinstance(inp, str):
        if inp == "foo":
            text = "ok-foo"
        elif inp == "discover":
            text = "ok-discover"
    return responses_factory.make_assistant_message(text)


@pytest.mark.parametrize(
    "openai_client_param",
    [pytest.param(_behavior_ok, id="mock"), pytest.param(LIVE, id="live", marks=pytest.mark.live_llm)],
    indirect=True,
)
async def test_prompt_eval_signals_tool_error_on_critic_error(
    openai_client_param, tmp_path: Path, make_typed_mcp
) -> None:
    # Build server with provided client (mock/live)
    mcp_server, _state = build_server(client=openai_client_param, name="prompt_eval_test", run_dir_base=tmp_path)

    # Patch _run_critic_for_specimen to raise within the server module
    async def _fake(specimen, system_prompt, client, run_dir, *, agent_model="gpt-5", **kwargs):
        raise RuntimeError("simulated critic failure")

    adgn.props.prompt_eval.server._run_critic_for_specimen = _fake
    # Limit to one real specimen to keep test deterministic and fast while exercising real data
    adgn.props.prompt_eval.server.list_specimen_names = lambda base: ["2025-09-02-ducktape_wt"]

    async with make_typed_mcp(mcp_server, "prompt_eval_test") as (client, _sess):
        # Expect an isError tool payload; assert via TypedClient.error
        payload = PromptEvalArgs(prompt="dummy")
        get_err = client.error("test_prompt")
        msg = await get_err(payload)
        assert "simulated critic failure" in msg


@pytest.mark.parametrize(
    "openai_client_param",
    [pytest.param(_behavior_ok, id="mock"), pytest.param(LIVE, id="live", marks=pytest.mark.live_llm)],
    indirect=True,
)
async def test_prompt_eval_returns_metrics_on_success(openai_client_param, tmp_path: Path, make_typed_mcp) -> None:
    # Build server with provided client (mock/live)
    mcp_server, _state = build_server(client=openai_client_param, name="prompt_eval_test2", run_dir_base=tmp_path)

    # Patch _run_critic_for_specimen to return a minimal CriticSubmitPayload instance
    async def _fake_ok(specimen, system_prompt, client, run_dir, *, agent_model="gpt-5", **kwargs):
        return CriticSubmitPayload(issues=[], notes_md=None)

    adgn.props.prompt_eval.server._run_critic_for_specimen = _fake_ok
    # Limit to one real specimen for a focused test run
    adgn.props.prompt_eval.server.list_specimen_names = lambda base: ["2025-09-02-ducktape_wt"]

    async with make_typed_mcp(mcp_server, "prompt_eval_test2") as (client, _sess):
        payload = PromptEvalArgs(prompt="dummy")
        out = await client.test_prompt(payload)
        assert isinstance(out, PromptEvalOutput)
        assert isinstance(out.metrics, list)
