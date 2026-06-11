"""Claude Code runner implementation using Docker containers."""

import shutil
import tempfile
import time
import uuid
from pathlib import Path

import structlog
from claude_code_sdk import (
    AssistantMessage as ClaudeAssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from inop.engine.models import (
    AssistantMessage,
    Rollout,
    RunnerEnvironment,
    SeedTask,
    TaskDefinition,
    TaskSetup,
    TaskTypeConfig,
    ToolCall,
    ToolResult,
    TrajectoryItem,
    UserInput,
    WorkspaceEnvironment,
)
from inop.io.file_utils import collect_docker_files
from inop.runners.base import AgentRunner
from inop.runners.containerized_claude import TaskClaude


class ClaudeRunner(AgentRunner):
    """Runner for Claude Code using Docker containers."""

    def __init__(self, runner_id: str, config: dict):
        """Initialize Claude runner.

        Args:
            runner_id: Unique identifier for this runner instance
            config: Claude-specific configuration including:
                - max_turns: Maximum conversation turns
                - bash_timeout_ms: Timeout for bash commands
                - strace_enabled: Whether to enable strace debugging
        """
        super().__init__(runner_id, config)
        self.workspace_path: Path | None = None  # explicit for mypy when base not resolved
        self.task_claude: TaskClaude | None = None
        self.current_task: TaskDefinition | None = None
        # Docker image will be set from task setup during setup()
        # TODO: Consider supporting (task, runner) specific Docker configs in the future
        self.docker_image: str | None = None

    async def setup(self, task: TaskDefinition, task_type_config: dict) -> None:
        """Set up workspace for task execution.

        Args:
            task: Task to execute
            task_type_config: Configuration from task type
        """
        # TaskTypeConfig mapping requires TaskTypeConfig values, not raw dicts
        ttype = TaskTypeConfig(name=task.type, grading=task_type_config.get("grading"))
        setup, _ = task.resolve_config({task.type: ttype})

        # Create workspace directory

        self.workspace_path = Path(tempfile.mkdtemp(prefix="claude_workspace_"))

        # Handle the new composite TaskSetup
        if isinstance(setup, TaskSetup):
            # Clone repository FIRST if needed (before Docker)
            if setup.git_clone:
                # Clone into the workspace from host (which has SSH keys)
                await self._clone_repository(setup.git_clone, str(self.workspace_path), is_docker=False)

            # Determine Docker image
            if setup.docker:
                self.docker_image = setup.docker.image
            else:
                # Default image if no Docker specified but we still need a container
                self.docker_image = self.config.get("default_docker_image", "ubuntu:22.04")
        else:
            # No setup - shouldn't happen for Claude runner
            self.docker_image = self.config.get("default_docker_image", "ubuntu:22.04")

        self.current_task = task

    async def run_task(self, task: TaskDefinition, agent_instructions: str) -> Rollout:
        """Execute task using Claude Code in container.

        Args:
            task: Task to execute (contains the task prompt)
            agent_instructions: The instructions being optimized (CLAUDE.md content)

        Returns:
            Rollout with trajectory and files
        """
        if not self.workspace_path:
            raise RuntimeError("Workspace not set up. Call setup() first.")

        # Create a SeedTask for TaskClaude compatibility
        seed_task = SeedTask(
            id=task.id,
            prompt=task.prompt,
            docker_image=self.docker_image,
            allowed_tools=task.allowed_tools,
            pre_task_commands=task.pre_task_commands,
        )

        # Create a simple logger

        logger = structlog.get_logger().bind(task_id=task.id, runner_id=self.runner_id)

        trajectory: list[TrajectoryItem] = []
        start_time = time.perf_counter()
        total_cost: float = 0.0
        success = True
        error_message: str | None = None

        try:
            # Use TaskClaude as context manager for proper container lifecycle
            async with TaskClaude(
                task_id=task.id, config=self.config, output_dir=self.workspace_path, seed_task=seed_task, logger=logger
            ) as claude:
                # Write agent instructions as CLAUDE.md
                claude.setup_system_prompt(agent_instructions)

                # Add user input to trajectory
                trajectory.append(UserInput(text=task.prompt))

                # Execute Claude session
                await claude.query()

                # Stream messages and build trajectory
                async for message in claude.receive_messages():
                    # Convert Claude SDK messages to our trajectory items
                    if isinstance(message, ClaudeAssistantMessage):
                        # Extract text from assistant message
                        text = ""
                        content = message.content
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            # Extract text from content blocks (only TextBlock has .text)
                            text_parts: list[str] = []
                            for block in content:
                                if isinstance(block, TextBlock):
                                    text_parts.append(block.text)
                            text = "\n".join(text_parts)

                        if text:
                            trajectory.append(AssistantMessage(text=text, original=message))

                    elif isinstance(message, ToolUseBlock):
                        # Tool call from Claude
                        trajectory.append(ToolCall(tool_name=message.name, arguments=message.input, original=message))

                    elif isinstance(message, ToolResultBlock):
                        # Tool result
                        trajectory.append(
                            ToolResult(
                                tool_name="",  # Claude doesn't provide tool name in result
                                result=message.content,
                                original=message,
                            )
                        )

                    elif isinstance(message, ResultMessage):
                        # Final result message with cost and status
                        if message.is_error:
                            success = False
                            # SDK does not expose a typed error message attribute; use a generic string
                            error_message = "Claude reported an error"

                        # total_cost_usd may be Optional; coerce to float
                        total_cost = float(message.total_cost_usd or 0.0)

                        # ResultMessage indicates completion
                        break

                # Collect files from workspace
                file_collection = await claude.collect_outputs()
                files = collect_docker_files(file_collection)

        except Exception as e:
            success = False
            error_message = str(e)
            logger.error("Claude execution failed", error=str(e))
            files = {}

        duration = time.perf_counter() - start_time

        return Rollout(
            task_id=task.id,
            runner_id=self.runner_id,
            agent_id=f"{self.runner_id}_{uuid.uuid4().hex[:8]}",
            trajectory=trajectory,
            files=files,
            success=success,
            error_message=error_message,
            cost_usd=total_cost,
            duration_seconds=duration,
            metadata={"workspace": str(self.workspace_path), "docker_image": self.docker_image},
        )

    async def cleanup(self) -> None:
        """Clean up workspace directory."""
        if self.workspace_path and self.workspace_path.exists():
            try:
                shutil.rmtree(self.workspace_path)
            except Exception as e:
                print(f"Error cleaning up workspace: {e}")
            self.workspace_path = None

        self.current_task = None
        self.docker_image = None

    def get_environment(self) -> RunnerEnvironment | None:
        """Get workspace environment information."""
        if not self.workspace_path:
            return None

        return WorkspaceEnvironment(workspace_path=self.workspace_path)
