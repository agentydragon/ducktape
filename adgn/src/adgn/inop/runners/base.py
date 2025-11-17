"""Base class for agent runners."""

from abc import ABC, abstractmethod
import asyncio
from pathlib import Path

from adgn.inop.engine.models import GitCloneConfig, Rollout, RunnerEnvironment, TaskDefinition
from adgn.inop.io.logging_utils import DualOutputLogging


class AgentRunner(ABC):
    """Base class for all agent runners.

    Runners are responsible for executing tasks and returning rollouts.
    They don't know about grading - that's handled separately based on task type.
    """

    def __init__(self, runner_id: str, config: dict):
        """Initialize runner with configuration.

        Args:
            runner_id: Unique identifier for this runner instance
            config: Runner-specific configuration
        """
        self.runner_id = runner_id
        self.config = config
        self.workspace_path: Path | None = None
        self.logger = DualOutputLogging.get_logger(f"runner.{runner_id}")

    @abstractmethod
    async def setup(self, task: TaskDefinition, task_type_config: dict) -> None:
        """Set up the runner for a specific task.

        Args:
            task: Task to execute
            task_type_config: Configuration from task type (setup, grading, etc.)
        """

    @abstractmethod
    async def run_task(self, task: TaskDefinition, agent_instructions: str) -> Rollout:
        """Execute the task and return a rollout.

        Args:
            task: Task to execute (contains the task prompt)
            agent_instructions: The instructions being optimized (e.g., CLAUDE.md content)

        Returns:
            Rollout with trajectory, files, and metadata
        """

    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up any resources used by the runner."""

    @abstractmethod
    def get_environment(self) -> RunnerEnvironment | None:
        """Get environment information for grading.

        Returns:
            RunnerEnvironment with type and data, or None if no environment.
        """

    async def _clone_repository(self, git_setup: GitCloneConfig, target_dir: str, is_docker: bool = False) -> None:
        """Clone a git repository using shallow clone to specific commit.

        This method clones directly into the target directory (workspace root),
        not into a subdirectory. The agent will start in the cloned repository.

        Args:
            git_setup: Git clone configuration with repo, commit, and optional subdir
            target_dir: Directory to clone into (/workspace for Docker, workspace_path for local)
            is_docker: Whether cloning is happening inside Docker container (True) or on host (False)
        """
        self.logger.info(
            "Cloning repository",
            repo=git_setup.repo,
            commit=git_setup.commit,
            target_dir=target_dir,
            clone_location="docker" if is_docker else "host",
        )

        # Commands for shallow clone to specific commit
        # We init, add remote, fetch specific commit, then checkout
        commands = [
            ["git", "init"],
            [
                "git",
                "config",
                "--local",
                "--add",
                "safe.directory",
                target_dir,
            ],  # Fix ownership issues in Docker (local only!)
            ["git", "remote", "add", "origin", git_setup.repo],
            ["git", "fetch", "--depth", "1", "origin", git_setup.commit],
            ["git", "checkout", "FETCH_HEAD"],
        ]

        for cmd in commands:
            if is_docker:
                # Run in Docker container - subclasses must implement this
                exit_code, _stdout, stderr = await self._run_docker_command(cmd, target_dir, timeout_s=60)
            else:
                # Run locally (asyncio subprocess)
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=target_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                _stdout_b, _stderr_b = await proc.communicate()
                # returncode is guaranteed non-None after communicate() completes per asyncio docs
                assert proc.returncode is not None, "returncode should be set after communicate()"
                exit_code = proc.returncode
                stderr = _stderr_b.decode() if _stderr_b else ""

            if exit_code != 0:
                self.logger.error("Git command failed", command=" ".join(cmd), exit_code=exit_code, stderr=stderr)
                raise RuntimeError(f"Failed to run {' '.join(cmd)}: {stderr}")

        self.logger.info("Repository cloned successfully")

        # Note: We don't handle git_setup.subdir - the agent can navigate
        # to any subdirectory as needed using shell commands

    async def _run_docker_command(self, cmd: list[str], cwd: str, timeout_s: int) -> tuple[int, str, str]:
        """Run a command in Docker container. Subclasses must implement if using Docker.

        Args:
            cmd: Command to run as list of strings
            cwd: Working directory in container
            timeout_s: Timeout in seconds

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        raise NotImplementedError("Subclass must implement _run_docker_command if using Docker")
