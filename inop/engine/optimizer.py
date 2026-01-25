"""Parallel prompt optimization system for coding agents.

Iteratively improves CLAUDE.md by running multiple agent rollouts in parallel
on seed programming tasks, grading solutions using OpenAI's Responses API (o3 model),
and using a PromptEngineer to propose improved prompts. All data is logged to JSONL
files for analysis.

Usage:
    python3 prompt_engineer_algorithm.py [--iterations N] [--rollouts-per-task N]

    --iterations N          Number of optimization iterations (default: 10)
    --rollouts-per-task N   Number of agent rollouts per seed task (default: 5)

Key Features:
* Fully parallel rollouts with semaphore-based concurrency control
* Docker containerization for isolated coding agent execution
* OpenAI Responses API integration for grading and prompt engineering
* PromptEngineer class with persistent conversation state and context trimming
* Structured logging with JSON output for comprehensive tracking

Architecture:
* Coding agents run in isolated Docker containers for safety
* Rollouts execute in parallel (configurable max concurrency)
* OpenAI o3 model handles both grading and prompt engineering
* Context trimming preserves reasoning token validity
* JSONL logs capture all API interactions and results

Configuration:
* Parallelism: Configurable via OptimizerConfig.max_parallel_rollouts (default: 8)
* Context limits: PromptEngineer handles 200k token o3 context automatically
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiodocker
from fastmcp.client import Client

from agent_core.agent import Agent
from agent_core.loop_control import RequireAnyTool
from agent_core.mcp_provider import MCPToolProvider
from agent_core.transcript_handler import TranscriptHandler
from inop.config import OptimizerConfig
from inop.engine import runner_factory
from inop.engine.models import AgentTaskType, Criterion, GradedRollout, TaskDefinition
from inop.grading.grader import grade_rollout
from inop.io.jsonl_logger import JSONLLogger
from inop.io.logging_utils import DualOutputLogging
from inop.io.task_loader import load_runner_configs, load_task_definitions, load_task_types
from inop.io.yaml_loader import load_yaml_files
from inop.mcp.prompt_feedback_server import make_prompt_feedback_server_with_state
from inop.model_factory import create_optimizer_models
from inop.plots import ScoreEvolutionTracker
from inop.prompting.pe_controller import ProposePromptNTimes
from inop.prompting.prompt_engineer import (
    FeedbackMode,
    FeedbackProvider,
    FullRolloutsFeedbackProvider,
    StatsOnlyFeedbackProvider,
)
from inop.prompting.summarizer import PatternSummarizer
from inop.prompting.truncation_utils import TruncationManager
from mcp_infra.compositor.compositor import Compositor
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import OpenAIModelProto, UserMessage

# TODO: consider showing grader text Assistant messages, not just code
# TODO: track exact OpenAI & Anthropic model used in database tables


# Database removed - using JSON files instead

# MCP-based PE wiring (new path)

# Module logger; handler config is applied by DualOutputLogging.setup_logging()
logger = logging.getLogger(__name__)

# MCP mount prefix for prompt feedback server
PROMPT_FEEDBACK_MOUNT_PREFIX = MCPMountPrefix("prompt_feedback")

# Global trackers
score_tracker = ScoreEvolutionTracker()


# Global cost tracking
@dataclass
class CostTracker:
    """Tracks total costs across all coding agent rollouts."""

    total_cost_usd = 0.0
    rollout_count = 0

    def add_rollout_cost(self, cost_usd: float):
        """Add cost from a completed rollout."""
        self.total_cost_usd += cost_usd
        self.rollout_count += 1
        logger.info(
            "Rollout cost added",
            extra={
                "rollout_cost_usd": cost_usd,
                "total_cost_usd": self.total_cost_usd,
                "rollout_count": self.rollout_count,
            },
        )

    def report_final_cost(self):
        """Report final cost summary."""
        logger.info(
            "FINAL COST SUMMARY",
            extra={
                "total_cost_usd": self.total_cost_usd,
                "rollout_count": self.rollout_count,
                "avg_cost_per_rollout_usd": self.total_cost_usd / max(1, self.rollout_count),
            },
        )


# Global cost tracker instance
cost_tracker = CostTracker()


def setup_signal_handlers():
    """Setup signal handlers for graceful cost reporting on interruption."""

    def signal_handler(signum, _frame):
        logger.info("Interrupt received, reporting costs before exit", extra={"signal": signum})
        cost_tracker.report_final_cost()
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


# -----------------------------------------------------------------------------
# MCP-driven Prompt Optimization (PE as MCP client)
# -----------------------------------------------------------------------------


@dataclass
class OptimizeMcpArgs:
    anthropic_log: JSONLLogger
    pe_model: OpenAIModelProto
    runner_model: OpenAIModelProto
    grader_model: OpenAIModelProto
    summarizer_model: OpenAIModelProto
    docker_client: aiodocker.Docker
    seed_tasks: list[TaskDefinition]
    criteria: list[Criterion]
    cfg: OptimizerConfig
    runner_name: str
    task_types: dict
    runner_configs: dict
    task_type: AgentTaskType
    iterations: int
    base_dir: Path


async def optimize_prompts_mcp(args: OptimizeMcpArgs) -> Path:
    """Run prompt optimization via an MCP server that evaluates prompts.

    The Prompt Engineer (Agent) will call propose_prompt(prompt) N times in one
    outer run; the MCP server will run rollouts+grading+persistence and maintain
    per-session state (last_prompt, last_feedback). We return the output dir.
    """

    # Choose feedback provider per config
    feedback_mode = FeedbackMode(args.cfg.prompt_engineer.feedback_mode)
    if feedback_mode == FeedbackMode.SUMMARY:
        feedback_provider: FeedbackProvider = PatternSummarizer(
            model=args.summarizer_model,
            context_model_id=args.cfg.summarizer.model,
            truncation_manager=TruncationManager(args.cfg),
            max_file_size_pattern_analysis=args.cfg.truncation.max_file_size_pattern_analysis,
        )
    elif feedback_mode == FeedbackMode.STATS_ONLY:
        feedback_provider = StatsOnlyFeedbackProvider()
    elif feedback_mode == FeedbackMode.FULL_ROLLOUTS:
        feedback_provider = FullRolloutsFeedbackProvider()
    else:
        raise ValueError(f"Invalid {feedback_mode = }.")

    # Deps to run rollouts for a given prompt
    class _Deps:
        def __init__(self):
            self._prompts: list[str] = []

        async def select_seed_tasks(self) -> list[TaskDefinition]:
            return args.seed_tasks

        async def run_rollouts_with_prompt(self, prompt: str, tasks: list[TaskDefinition]) -> list[GradedRollout]:
            print(f"[_Deps.run_rollouts_with_prompt] start, tasks={len(tasks)} prompt={prompt}")
            # Minimal serial implementation (can parallelize later)
            results: list[GradedRollout] = []
            for t in tasks:
                print(f"[_Deps.run_rollouts_with_prompt] setting up runner for task {t.id}")
                # Create runner with the configured OpenAI model
                runner_model = args.runner_model
                runner = runner_factory.create_runner(
                    args.runner_name, args.runner_configs, openai_model=runner_model, docker_client=args.docker_client
                )
                # Prepare task-type specific setup
                if t.type not in args.task_types:
                    raise ValueError(f"Unknown task type: {t.type}")
                task_type_config = args.task_types[t.type]
                await runner.setup(t, task_type_config)

                # Single rollout per task (id=0)
                rollout = await runner.run_task(t, agent_instructions=prompt)
                print(f"[_Deps.run_rollouts_with_prompt] got rollout for task {t.id}")

                # Grade rollout
                _, grading_config = t.resolve_config(args.task_types)
                if grading_config is None:
                    raise ValueError("grading_config is required for grade_rollout")
                grade = await grade_rollout(
                    rollout=rollout,
                    task=t,
                    grading_config=grading_config,
                    model=args.grader_model,
                    cfg=args.cfg,
                    environment=runner.get_environment(),
                )
                print(f"[_Deps.run_rollouts_with_prompt] got grade for task {t.id}")
                # Package graded rollout (include task per model schema)
                results.append(GradedRollout(rollout=rollout, grade=grade, task=t))
                await runner.cleanup()
                print(f"[_Deps.run_rollouts_with_prompt] cleaned up runner for task {t.id}")
            return results

        def persist_all(self, *, iteration: int, prompt: str, rollouts: list[GradedRollout], feedback: str) -> None:
            print(f"[persist_all] iter={iteration} base_dir={args.base_dir}")
            it_dir = args.base_dir / f"iter_{iteration:03d}"
            it_dir.mkdir(parents=True, exist_ok=True)
            (it_dir / "CLAUDE.md").write_text(prompt)

            # Append to prompts.jsonl and feedback.jsonl (append-only logs)
            prompts_log = args.base_dir / "prompts.jsonl"
            feedback_log = args.base_dir / "feedback.jsonl"
            with prompts_log.open("a") as f:
                f.write(json.dumps({"iteration": iteration, "prompt": prompt}) + "\n")
            with feedback_log.open("a") as f:
                f.write(json.dumps({"iteration": iteration, "feedback": feedback}) + "\n")

            # Update prompts.json as a list built in-memory (no file read)
            self._prompts.append(prompt)
            (args.base_dir / "prompts.json").write_text(json.dumps(self._prompts, indent=2))

            # Persist each rollout and its grading under task/agent_0
            for gr in rollouts:
                t_id = gr.rollout.task_id
                rollout_dir = it_dir / t_id / "agent_0"
                rollout_dir.mkdir(parents=True, exist_ok=True)
                # rollout.json
                rollout_data = {
                    "task_id": t_id,
                    "agent_id": "agent_0",
                    "iteration": iteration,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "runner_id": gr.rollout.runner_id,
                    "success": gr.rollout.success,
                    "cost_usd": gr.rollout.cost_usd,
                    "duration_seconds": gr.rollout.duration_seconds,
                    "trajectory": [item.model_dump() for item in gr.rollout.trajectory],
                    "files": gr.rollout.files,
                    "metadata": gr.rollout.metadata,
                }
                (rollout_dir / "rollout.json").write_text(json.dumps(rollout_data, indent=2))
                # grading.json
                grading_data = {"overall_score": gr.grade.overall_score}
                (rollout_dir / "grading.json").write_text(json.dumps(grading_data, indent=2))

    deps = _Deps()

    # Build the MCP servers and session handle
    # Build secured in-proc attach via factory and capture the handle directly
    # Use Compositor as async context manager to ensure cleanup
    async with Compositor() as comp:
        _server, state = make_prompt_feedback_server_with_state(deps, feedback_provider)
        await comp.mount_inproc(PROMPT_FEEDBACK_MOUNT_PREFIX, _server)
        async with Client(comp) as mcp_client:
            # Create Agent PE with system prompt at init
            model = args.pe_model
            # Build the expert prompt-engineer system message (same wording formerly in PromptEngineer.prompt_messages)
            agent_description = "a coding agent"
            task_description = (
                "- Agent has access to a filesystem and a shell through tools. "
                "Tasks should be solved by writing code files on disk using these tools, "
                "not just shown to user in conversation.\n"
            )
            system_message = (
                "You are an expert LLM prompt engineer. "
                f"Your task is to design the best prompt for a LLM used as "
                f"{agent_description}.\n"
                f"{task_description}"
                "- Agent has a fixed system prompt that teaches it how to use its tools (and other basics).\n"
                "- Avoid giving your own instructions on how to use the tools - agent's baked-in tool use instructions "
                "are already correct and additional conflicting instructions could easily make it worse.\n"
                "- Each turn, you will propose a prompt. The agent will be run with that prompt on several tasks, "
                "and you will receive information from these rollouts to help you design a better prompt.\n"
                "- Your goal is to *find the best performing prompt you can* over *N turns* of (propose prompt1 -> "
                "receive feedback1 -> propose prompt2 -> ...). You will be scored by the max score, not the last score.\n"
                f"- The feedback will take the form of: {feedback_provider.verbal_description()}\n"
            )

            # Write PE transcripts into the main optimization output directory
            run_dir = args.base_dir
            run_dir.mkdir(parents=True, exist_ok=True)
            # TODO(mpokorny): AbortIf(max_iters) and current ProposePromptNTimes can be exceeded under
            # parallel_tool_calls if multiple calls are in flight when the budget flips. Centralize budget
            # accounting at the server boundary or serialize within 1 of the limit to enforce a hard cap.
            pe = await Agent.create(
                tool_provider=MCPToolProvider(mcp_client),
                client=model,
                handlers=[
                    ProposePromptNTimes(args.iterations),
                    TranscriptHandler(events_path=run_dir / "events.jsonl"),
                ],
                tool_policy=RequireAnyTool(),
            )

            # Insert user message with instructions
            pe.process_message(UserMessage.text(system_message))
            # Force N propose_prompt tool calls then abort (handled by ProposePromptNTimes registered above)
            await pe.run()

            # Read final state directly (in-proc) for logging only
            last_prompt = state.last_prompt or ""
            last_feedback = state.last_feedback or ""

            logger.info(
                "Optimization complete (MCP)",
                extra={
                    "last_prompt_preview": (last_prompt or "")[:160],
                    "last_feedback_preview": (last_feedback or "")[:160],
                },
            )
    # Compositor.__aexit__ unmounts all non-pinned servers and cleans up containers here
    return args.base_dir


# -----------------------------------------------------------------------------
# Helper functions for deduplication and common operations
# (file collection lives in core/file_ops; keep local helpers here)
# -----------------------------------------------------------------------------


@dataclass
class OptimizeArgs:
    anthropic_log: JSONLLogger
    # Required DI: adapter model instances for specific roles.
    pe_model: OpenAIModelProto
    runner_model: OpenAIModelProto
    grader_model: OpenAIModelProto
    summarizer_model: OpenAIModelProto
    docker_client: aiodocker.Docker
    seed_tasks: list[TaskDefinition]
    criteria: list[Criterion]
    cfg: OptimizerConfig
    runner_name: str
    task_types: dict
    runner_configs: dict
    task_type: AgentTaskType  # Type of agent being optimized
    base_dir: Path
    iterations: int = 3
    rollouts_per_task: int = 2
    max_parallel_rollouts: int | None = None
    tasks_per_iteration: int | None = None


async def optimize_prompts(args: OptimizeArgs) -> Path:
    """Run the prompt optimisation loop.

    This is the main entry point for running multiple iterations of the algorithm. It
    repeatedly executes batches of agents on the seed tasks in parallel,
    grades the generated solutions using OpenAI's Responses API, updates the system
    prompt using a prompt engineer (OpenAI o3), and logs results to JSONL files.

    Parameters
    ----------
    seed_tasks : List[TaskDefinition]
        The tasks to use as the benchmark for optimisation.
    criteria : List[Criterion]
        Grading criteria to evaluate task performance.
    cfg : OptimizerConfig
        Configuration for the optimizer.
    runner_name : str
        Name of the runner to use (e.g., "claude", "agent").
    task_types : dict
        Task type configurations.
    runner_configs : dict
        Runner configurations.
    iterations : int, optional
        The number of optimisation iterations to perform (default 3).
    rollouts_per_task : int, optional
        The number of agents to sample per task in each iteration (default 2).
    max_parallel_rollouts : int, optional
        Maximum number of concurrent agent rollouts (default from config).
    tasks_per_iteration : int, optional
        Number of tasks to randomly sample (with replacement) per iteration.
        If None, uses all seed tasks (default None).
    base_dir : Path
        Base directory for output.
    """
    # Delegate to MCP-driven implementation (single source of truth)
    return await optimize_prompts_mcp(
        OptimizeMcpArgs(
            anthropic_log=args.anthropic_log,
            pe_model=args.pe_model,
            runner_model=args.runner_model,
            grader_model=args.grader_model,
            summarizer_model=args.summarizer_model,
            docker_client=args.docker_client,
            seed_tasks=args.seed_tasks,
            criteria=args.criteria,
            cfg=args.cfg,
            runner_name=args.runner_name,
            task_types=args.task_types,
            runner_configs=args.runner_configs,
            task_type=args.task_type,
            iterations=args.iterations,
            base_dir=args.base_dir,
        )
    )


def main() -> None:
    """Entry point for standalone execution."""
    parser = argparse.ArgumentParser(
        description="Parallel prompt optimization system for coding agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --iterations 5 --rollouts-per-task 3
  %(prog)s --mode summary --iterations 1 --rollouts-per-task 1
  %(prog)s --mode stats_only --iterations 3 --rollouts-per-task 2
        """,
    )

    parser.add_argument(
        "--iterations", type=int, default=10, help="Number of optimization iterations (default: %(default)s)"
    )

    parser.add_argument(
        "--rollouts-per-task", type=int, default=1, help="Number of agent rollouts per seed task (default: %(default)s)"
    )

    parser.add_argument(
        "--max-parallel", type=int, default=None, help="Maximum parallel rollouts (default: from config file)"
    )

    parser.add_argument(
        "--tasks-per-iteration",
        type=int,
        default=None,
        help=(
            "Number of tasks to randomly sample (with replacement) per iteration. If not specified, uses all seed tasks"
        ),
    )

    parser.add_argument(
        "--runner", type=str, default="claude", help="Runner to use for task execution (default: %(default)s)"
    )

    parser.add_argument(
        "--verbose", action="store_true", help="Enable verbose logging for agent actions (mini_codex only)"
    )

    parser.add_argument(
        "--task-type",
        type=str,
        required=True,
        choices=[t.value for t in AgentTaskType],
        help="Type of tasks to optimize for (required)",
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        required=True,
        help="Directory containing config.yaml, task_types.yaml, runners.yaml (all loaded from here)",
    )

    args = parser.parse_args()

    # Setup logging with verbosity setting
    DualOutputLogging.setup_logging(verbose=args.verbose)
    # Module-level logger inherits root handlers configured above

    # Setup signal handlers for graceful cost reporting on interruption
    setup_signal_handlers()

    # Load ALL configuration explicitly from --config-dir
    config_dir = args.config_dir
    cfg_path = config_dir / "config.yaml"
    cfg = OptimizerConfig.from_file(cfg_path)

    # Resolve seeds/graders relative to config_dir when relative paths are provided
    seeds_path = Path(cfg.seeds_file)
    if not seeds_path.is_absolute():
        seeds_path = (config_dir / seeds_path).resolve()
    graders_path = Path(cfg.graders_file)
    if not graders_path.is_absolute():
        graders_path = (config_dir / graders_path).resolve()

    # Load task types and runner configurations from explicit directory
    task_types = load_task_types(config_dir / "task_types.yaml")
    runner_configs = load_runner_configs(config_dir / "runners.yaml")

    # Load tasks from seeds file - now using TaskDefinition format
    all_tasks = load_task_definitions(seeds_path, task_types)

    # Convert string to AgentTaskType enum
    task_type_enum = AgentTaskType(args.task_type)

    # Filter tasks by type
    seed_tasks = [t for t in all_tasks if t.type == task_type_enum.value]

    if not seed_tasks:
        logger.error(f"No tasks found with type '{task_type_enum.value}' in {seeds_path}")
        sys.exit(1)

    logger.info(f"Loaded {len(seed_tasks)} {task_type_enum.value} tasks from {len(all_tasks)} total tasks")

    # Load grading criteria from YAML
    logger.info("Loading grading criteria")
    yaml_loader = load_yaml_files(seeds_path, graders_path)

    # Load grading criteria
    criteria = []
    for grader_data in yaml_loader.graders_data:
        criteria.append(Criterion(name=grader_data.id, description=grader_data.description))

    run_prefix = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    base_dir = (Path("./agent_output") / run_prefix).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    # Create OpenAI client and DI models
    anthropic_log = JSONLLogger(base_dir / "anthropic_api_log.jsonl")
    # Build OpenAI models with debug logging enabled (HTTP frames captured to logger)
    models = create_optimizer_models(cfg, enable_debug_logging=True)
    # Create async Docker client for runner
    docker_client = aiodocker.Docker()
    try:
        run_dir = asyncio.run(
            optimize_prompts(
                OptimizeArgs(
                    anthropic_log=anthropic_log,
                    pe_model=models.pe_model,
                    runner_model=models.runner_model,
                    grader_model=models.grader_model,
                    summarizer_model=models.summarizer_model,
                    docker_client=docker_client,
                    seed_tasks=seed_tasks,
                    criteria=criteria,
                    cfg=cfg,
                    runner_name=args.runner,
                    task_types=task_types,
                    runner_configs=runner_configs,
                    task_type=task_type_enum,
                    iterations=args.iterations,
                    rollouts_per_task=args.rollouts_per_task,
                    max_parallel_rollouts=args.max_parallel,
                    tasks_per_iteration=args.tasks_per_iteration,
                    base_dir=base_dir,
                )
            )
        )
    finally:
        asyncio.run(docker_client.close())

    # Generate final score evolution report
    final_evolution_report = score_tracker.generate_report(run_dir, run_dir)
    final_report_path = run_dir / "final_score_evolution_report.txt"
    final_report_path.write_text(final_evolution_report)

    print("\n" + "=" * 60)
    print(final_evolution_report)
    print("=" * 60)

    logger.info(
        "Score evolution report generated", extra={"report_path": str(final_report_path), "run_directory": str(run_dir)}
    )
    cost_tracker.report_final_cost()


if __name__ == "__main__":
    main()
