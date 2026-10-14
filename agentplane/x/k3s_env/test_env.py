"""A real Qwen turn through the app, a Pod Sandbox and the public LiteLLM, on the bare-k3s stack."""

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import pytest_bazel

from agentplane.acceptance.agent import Agent
from agentplane.acceptance.conftest import POD_TEMPLATE, wait_until_running
from agentplane.app.sandbox_models import NewSandbox
from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.runner.harness import Harness
from agentplane.x.k3s_env import render
from agentplane.x.k3s_env.stack import Stack, run, running_stack

# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

SANDBOX = "smoke"


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack(litellm_key=os.environ["LITELLM_QWEN_KEY"]) as running:
        yield running


def _print_turn_diagnostics(stack: Stack) -> None:
    """The error alone does not say which hop refused."""
    for selector in (
        "app.kubernetes.io/name=agentplane-app",
        "app.kubernetes.io/name=agentplane-runner-gateway",
        "app.kubernetes.io/name=agentplane-llm-ingress",
        f"agentplane.allegedly.works/sandbox={SANDBOX}",
    ):
        logs = stack.k3s.kubectl(
            "logs", "-l", selector, "-n", render.NAMESPACE, "--all-containers", "--tail=60", check=False
        )
        print(f"--- {selector}\n{logs}")
    print(
        "--- LiteLLM from the host\n",
        run("curl", "-sS", "-m", "20", "-I", f"https://{render.LITELLM_EXTERNAL_HOST}/", check=False),
    )


async def test_qwen_turn_completes(stack: Stack) -> None:
    async with Client(base_url=stack.base_url, token=stack.token) as client:
        model = (await client.models()).harnesses[Harness.CLAUDE][0]
        await client.create_sandbox(NewSandbox(name=SANDBOX, template=POD_TEMPLATE))
        await wait_until_running(client, SANDBOX)
        agent = await Agent.open(client, sandbox=SANDBOX, harness=runner_pb2.HARNESS_CLAUDE, model=model)
        try:
            turn = await agent.run("Reply with the single word: pong")
        except httpx.HTTPStatusError, AssertionError:
            _print_turn_diagnostics(stack)
            raise
    print("answer:", turn.answer)
    (Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", "/tmp")) / "answer.txt").write_text(turn.answer)


if __name__ == "__main__":
    pytest_bazel.main()
