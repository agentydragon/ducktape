"""Grocy MCP eval test: run an agent against a real Grocy instance.

Live-only test requiring OPENAI_API_KEY (or ANTHROPIC_API_KEY for --api=anthropic).
The full transcript is saved as an undeclared test output for human inspection.
"""

from __future__ import annotations

import logging
import os

import pytest
import pytest_bazel

from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.grocy_mcp.eval.run import DEFAULT_MODELS, run_grocy_eval

logger = logging.getLogger(__name__)


@pytest.mark.live_openai_api
async def test_grocy_eval(grocy_base_url: str) -> None:
    api = os.environ.get("EVAL_API", "openai")
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODELS[api])
    base_url = os.environ.get("OPENAI_BASE_URL")
    output_dir = undeclared_outputs_dir() / "grocy_eval"

    result = await run_grocy_eval(
        api=api,
        model=model,
        grocy_base_url=grocy_base_url,
        output_dir=output_dir,
        base_url=base_url,
    )

    assert result.transcript_path.exists(), f"Transcript not written: {result.transcript_path}"
    assert result.task_turns > 0, "Agent made no tool calls"
    logger.info("Eval complete: %d task turns, model=%s", result.task_turns, result.model)
    logger.info("Postmortem:\n%s", result.postmortem_text[:2000])


if __name__ == "__main__":
    pytest_bazel.main()
