"""Factory for creating agent runners."""

import aiodocker

from openai_utils.model import OpenAIModelProto
from x.inop.engine.models import ClaudeRunnerConfig, MinicodexRunnerConfig, RunnerConfig
from x.inop.runners.base import AgentRunner
from x.inop.runners.claude_runner import ClaudeRunner
from x.inop.runners.openai_runner import OpenAIRunner


def create_runner(
    runner_name: str,
    runner_configs: dict[str, RunnerConfig],
    openai_model: OpenAIModelProto | None = None,
    docker_client: aiodocker.Docker | None = None,
) -> AgentRunner:
    """Create an agent runner based on configuration.

    Args:
        runner_name: Name of the runner (e.g., "claude", "agent")
        runner_configs: Runner configurations loaded from runners.yaml
        openai_client: (deprecated) removed; pass OpenAIModelProto via openai_model

    Raises:
        ValueError: If runner type is unknown
    """
    if runner_name not in runner_configs:
        raise ValueError(f"Unknown runner: {runner_name}")

    runner_config = runner_configs[runner_name]

    match runner_config:
        case ClaudeRunnerConfig():
            return ClaudeRunner(runner_id=runner_name, config=runner_config.config.model_dump())
        case MinicodexRunnerConfig():
            if openai_model is None:
                raise ValueError("OpenAIRunner requires openai_model")
            if docker_client is None:
                raise ValueError("OpenAIRunner requires docker_client")
            return OpenAIRunner(
                runner_id=runner_name,
                config=runner_config.config.model_dump(),
                openai_model=openai_model,
                docker_client=docker_client,
            )
        case _:
            raise TypeError(f"unsupported runner config: {type(runner_config).__name__}")
