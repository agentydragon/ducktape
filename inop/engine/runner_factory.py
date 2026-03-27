"""Factory for creating agent runners."""

from typing import Any

import aiodocker

from inop.runners.base import AgentRunner
from inop.runners.claude_runner import ClaudeRunner
from inop.runners.openai_runner import OpenAIRunner
from openai_utils.model import OpenAIModelProto


def create_runner(
    runner_name: str,
    runner_configs: dict[str, dict[str, Any]],
    openai_model: OpenAIModelProto | None = None,
    docker_client: aiodocker.Docker | None = None,
) -> AgentRunner:
    """Create an agent runner based on configuration.

    Args:
        runner_name: Name of the runner (e.g., "claude", "agent")
        runner_configs: Dictionary of runner configurations from runners.yaml
        openai_client: (deprecated) removed; pass OpenAIModelProto via openai_model

    Returns:
        Instantiated runner

    Raises:
        ValueError: If runner type is unknown
    """
    if runner_name not in runner_configs:
        raise ValueError(f"Unknown runner: {runner_name}")

    runner_config = runner_configs[runner_name]
    runner_type = runner_config["type"]
    config = runner_config.get("config", {})

    # Create runner based on type
    if runner_type == "claude_runner":
        return ClaudeRunner(runner_id=runner_name, config=config)
    if runner_type == "openai_runner":
        if openai_model is None:
            raise ValueError("OpenAIRunner requires openai_model")
        if docker_client is None:
            raise ValueError("OpenAIRunner requires docker_client")
        return OpenAIRunner(
            runner_id=runner_name, config=config, openai_model=openai_model, docker_client=docker_client
        )
    raise ValueError(f"Unknown runner type: {runner_type}")
