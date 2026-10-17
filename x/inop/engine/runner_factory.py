"""Factory for creating agent runners."""

from x.inop.engine.models import ClaudeRunnerConfig, RunnerConfig
from x.inop.runners.base import AgentRunner
from x.inop.runners.claude_runner import ClaudeRunner


def create_runner(runner_name: str, runner_configs: dict[str, RunnerConfig]) -> AgentRunner:
    """Create an agent runner based on configuration.

    Args:
        runner_name: Name of the configured runner (currently "claude")
        runner_configs: Runner configurations loaded from runners.yaml
    Raises:
        ValueError: If runner type is unknown
    """
    if runner_name not in runner_configs:
        raise ValueError(f"Unknown runner: {runner_name}")

    runner_config = runner_configs[runner_name]

    match runner_config:
        case ClaudeRunnerConfig():
            return ClaudeRunner(runner_id=runner_name, config=runner_config.config.model_dump())
        case _:
            raise TypeError(f"unsupported runner config: {type(runner_config).__name__}")
