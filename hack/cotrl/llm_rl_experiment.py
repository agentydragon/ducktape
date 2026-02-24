#!/usr/bin/env python3
"""Test language models as RL agents with raw numerical data."""

import asyncio
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import aiofiles
import gymnasium as gym
import matplotlib.pyplot as plt
import numpy as np
import openai
from pydantic import BaseModel, Field
from scipy import stats

# Models to test
MODELS = ["gpt-4o-mini", "gpt-4o", "o1-mini"]

# Environments (selected for tensor representability)
ENVIRONMENTS = [
    "FrozenLake-v1",  # Discrete(16) state, Discrete(4) actions
    "CliffWalking-v1",  # Discrete(48) state, Discrete(4) actions
    "CartPole-v1",  # Box(4) state, Discrete(2) actions
    "MountainCar-v0",  # Box(2) state, Discrete(3) actions
    "Blackjack-v1",  # Tuple(3) state, Discrete(2) actions
    "Acrobot-v1",  # Box(6) state, Discrete(3) actions
]

# Experiment parameters
EPISODES_PER_RUN = 5
RUNS_PER_EXPERIMENT = 10
MAX_CONCURRENT_EXPERIMENTS = 30  # Limit concurrent API calls to avoid rate limits

# Environment-specific max steps (use gym defaults where appropriate)
MAX_STEPS = {
    "FrozenLake-v1": 100,  # Usually solved quickly or gets stuck
    "CliffWalking-v1": 200,  # Grid is 4x12, shouldn't need many steps
    "CartPole-v1": 500,  # Default gym limit
    "MountainCar-v0": 200,  # Default gym limit (though rarely solved)
    "Blackjack-v1": 100,  # Games are typically short
    "Acrobot-v1": 500,  # Default gym limit
}


class Message(BaseModel):
    """Single message in conversation"""

    role: Literal["system", "user", "assistant"]
    content: str


class EpisodeData(BaseModel):
    """Log entry for episode data"""

    timestamp: datetime = Field(default_factory=datetime.now)
    model: str
    environment: str
    run_num: int
    episode_num: int
    total_reward: float
    num_steps: int
    states: list[Any]  # Can be list or ndarray
    actions: list[int]
    rewards: list[float]


class StepData(BaseModel):
    """Log entry for step data"""

    timestamp: datetime = Field(default_factory=datetime.now)
    model: str
    environment: str
    run_num: int
    episode_num: int
    step_num: int
    state: Any  # Can be list or ndarray
    action: int
    reward: float
    done: bool
    truncated: bool


class SummaryData(BaseModel):
    """Experiment summary data"""

    experiment_start: datetime
    experiment_end: datetime = Field(default_factory=datetime.now)
    duration_seconds: float
    models: list[str]
    environments: list[str]
    episodes_per_run: int
    runs_per_experiment: int
    total_runs: int
    log_directory: str


@dataclass
class Step:
    state: Any
    action: int
    reward: float
    done: bool
    truncated: bool


@dataclass
class Episode:
    steps: list[Step]
    total_reward: float


@dataclass
class Run:
    episodes: list[Episode]
    model: str
    environment: str


class OpaqueEnvironmentWrapper:
    """Wraps a Gym environment to present only raw numerical data."""

    def __init__(self, env_name: str):
        self.env = gym.make(env_name)
        self.env_name = env_name
        self.action_space_size = self.env.action_space.n

    def reset(self) -> np.ndarray:
        obs, _info = self.env.reset()
        return self._flatten_observation(obs)

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool]:
        obs, reward, done, truncated, _info = self.env.step(action)
        return self._flatten_observation(obs), reward, done, truncated

    def _flatten_observation(self, obs: Any) -> np.ndarray:
        """Convert any observation to a flat numpy array."""
        if isinstance(obs, int):
            return np.array([obs], dtype=np.float32)
        if isinstance(obs, tuple):
            # For Blackjack's tuple observation
            return np.array(obs, dtype=np.float32)
        # Already a numpy array
        return obs.astype(np.float32)

    def close(self):
        self.env.close()


class ExperimentLogger:
    """Handles online logging of experiment data."""

    def __init__(self, experiment_name: str):
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_dir = Path(f"logs/{experiment_name}_{self.timestamp}")
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Create different log files
        self.episode_log = self.log_dir / "episodes.jsonl"
        self.step_log = self.log_dir / "steps.jsonl"
        self.summary_log = self.log_dir / "summary.json"

        # Track experiment start time
        self.start_time = datetime.now()

    async def log_episode(self, model: str, env_name: str, run_num: int, episode_num: int, episode: Episode):
        """Log episode data as it completes."""
        episode_data = EpisodeData(
            model=model,
            environment=env_name,
            run_num=run_num,
            episode_num=episode_num,
            total_reward=episode.total_reward,
            num_steps=len(episode.steps),
            states=[
                step.state.tolist() if isinstance(step.state, np.ndarray) else step.state for step in episode.steps
            ],
            actions=[step.action for step in episode.steps],
            rewards=[step.reward for step in episode.steps],
        )

        async with aiofiles.open(self.episode_log, "a") as f:
            await f.write(episode_data.model_dump_json() + "\n")

    async def log_step(self, model: str, env_name: str, run_num: int, episode_num: int, step_num: int, step: Step):
        """Log individual step data (optional, for detailed analysis)."""
        step_data = StepData(
            model=model,
            environment=env_name,
            run_num=run_num,
            episode_num=episode_num,
            step_num=step_num,
            state=step.state.tolist() if isinstance(step.state, np.ndarray) else step.state,
            action=step.action,
            reward=step.reward,
            done=step.done,
            truncated=step.truncated,
        )

        async with aiofiles.open(self.step_log, "a") as f:
            await f.write(step_data.model_dump_json() + "\n")

    async def log_summary(self, all_runs: list[Run]):
        """Log experiment summary."""
        summary = SummaryData(
            experiment_start=self.start_time,
            duration_seconds=(datetime.now() - self.start_time).total_seconds(),
            models=MODELS,
            environments=ENVIRONMENTS,
            episodes_per_run=EPISODES_PER_RUN,
            runs_per_experiment=RUNS_PER_EXPERIMENT,
            total_runs=len(all_runs),
            log_directory=str(self.log_dir),
        )

        async with aiofiles.open(self.summary_log, "w") as f:
            await f.write(summary.model_dump_json(indent=2))

    def get_log_directory(self) -> Path:
        """Return the log directory path."""
        return self.log_dir


class LLMRLAgent:
    """Agent that uses an LLM to select actions based on raw numerical data."""

    def __init__(self, model: str):
        self.model = model
        self.client = openai.AsyncOpenAI(
            timeout=30.0,  # 30 second timeout for API calls
            max_retries=2,  # Retry up to 2 times on failure
        )
        self.conversation_history: list[Message] = []

    def _create_initial_prompt(self) -> str:
        return """You are in an environment. Your goal is to maximize cumulative reward.

You will receive:
- State: A list of numbers representing your current state
- Reward: A number indicating your last reward (higher is better)
- Available actions: Integer choices you can make

Respond with ONLY a single integer representing your chosen action.

Let's begin."""

    def _create_step_prompt(
        self, state: np.ndarray, reward: float, action_space_size: int, first_step: bool = False
    ) -> str:
        state_str = np.array2string(state, precision=4, suppress_small=True)

        if first_step:
            return f"""State: {state_str}
Available actions: {", ".join(str(i) for i in range(action_space_size))}
Choose action:"""
        return f"""State: {state_str}
Last reward: {reward:.4f}
Available actions: {", ".join(str(i) for i in range(action_space_size))}
Choose action:"""

    async def get_action(
        self, state: np.ndarray, reward: float, action_space_size: int, first_step: bool = False
    ) -> int:
        """Get action from LLM based on raw state data."""
        if not self.conversation_history:
            self.conversation_history.append(Message(role="system", content=self._create_initial_prompt()))

        prompt = self._create_step_prompt(state, reward, action_space_size, first_step)
        self.conversation_history.append(Message(role="user", content=prompt))

        messages_dict = [msg.model_dump() for msg in self.conversation_history]
        if self.model == "o1-mini":
            # o1 models don't support temperature parameter
            response = await self.client.chat.completions.create(
                model=self.model, messages=messages_dict, max_tokens=10
            )
        else:
            response = await self.client.chat.completions.create(
                model=self.model, messages=messages_dict, temperature=1.0, max_tokens=10
            )

        action_str = response.choices[0].message.content.strip()
        action = int(action_str)

        # Validate action
        if 0 <= action < action_space_size:
            self.conversation_history.append(Message(role="assistant", content=action_str))
            return action

        raise ValueError(f"LLM returned invalid action {action} (must be 0-{action_space_size - 1})")

    def reset(self):
        """Reset conversation history for new episode."""
        self.conversation_history = []


async def run_episode(
    agent: LLMRLAgent,
    env: OpaqueEnvironmentWrapper,
    episode_num: int,
    model: str = "",
    exp_id: int = 0,
    logger: ExperimentLogger | None = None,
    run_num: int = 0,
) -> Episode:
    """Run a single episode with the agent."""
    prefix = f"[{exp_id}]" if exp_id > 0 else ""
    print(f"{prefix} {model} - {env.env_name} - Episode {episode_num + 1}/{EPISODES_PER_RUN}")

    agent.reset()
    state = env.reset()
    steps = []
    total_reward = 0.0

    # Get max steps for this environment
    max_steps = MAX_STEPS.get(env.env_name, 200)

    # First step
    action = await agent.get_action(state, 0.0, env.action_space_size, first_step=True)

    for _ in range(max_steps):
        # Take action
        next_state, reward, done, truncated = env.step(action)
        total_reward += reward

        steps.append(Step(state, action, reward, done, truncated))

        if done or truncated:
            break

        # Get next action
        state = next_state
        action = await agent.get_action(state, reward, env.action_space_size)

    print(
        f"{prefix} {model} - {env.env_name} - Episode {episode_num + 1} complete: {len(steps)} steps, reward={total_reward:.2f}"
    )

    episode = Episode(steps, total_reward)

    # Log episode data online
    if logger:
        await logger.log_episode(model, env.env_name, run_num, episode_num, episode)

    return episode


async def run_experiment(
    model: str,
    env_name: str,
    run_num: int,
    experiment_id: int = 0,
    total_experiments: int = 0,
    logger: ExperimentLogger | None = None,
) -> Run:
    """Run multiple episodes for one model on one environment."""
    agent = LLMRLAgent(model)
    env = OpaqueEnvironmentWrapper(env_name)

    episodes = []
    for episode_num in range(EPISODES_PER_RUN):
        try:
            # Add 120 second timeout per episode
            episode = await asyncio.wait_for(
                run_episode(agent, env, episode_num, model, experiment_id, logger, run_num), timeout=120.0
            )
            episodes.append(episode)
        except TimeoutError:
            print(f"[{experiment_id}] {model} - {env_name} - Episode {episode_num + 1} TIMEOUT after 120s")
            # Re-raise to fail the whole run
            raise

    env.close()
    return Run(episodes, model, env_name)


def plot_results(all_runs: list[Run]):
    """Create a grid of plots showing learning curves."""
    n_models = len(MODELS)
    n_envs = len(ENVIRONMENTS)

    _fig, axes = plt.subplots(n_models, n_envs, figsize=(4 * n_envs, 3 * n_models))
    if n_models == 1:
        axes = axes.reshape(1, -1)
    if n_envs == 1:
        axes = axes.reshape(-1, 1)

    # Organize data by model and environment
    results: dict[str, dict[str, list[list[float]]]] = defaultdict(lambda: defaultdict(list))

    for run in all_runs:
        # Extract cumulative rewards over time
        rewards_over_time: list[float] = []
        cumulative_reward = 0.0

        for episode in run.episodes:
            episode_rewards: list[float] = []
            ep_cumulative = cumulative_reward

            for step in episode.steps:
                ep_cumulative += step.reward
                episode_rewards.append(ep_cumulative)

            rewards_over_time.extend(episode_rewards)
            cumulative_reward = ep_cumulative

        results[run.model][run.environment].append(rewards_over_time)

    # Plot each model-environment combination
    for i, model in enumerate(MODELS):
        for j, env in enumerate(ENVIRONMENTS):
            ax = axes[i, j]

            # Get all runs for this combination
            runs_data = results[model][env]

            if runs_data:
                # Pad sequences to same length
                max_len = max(len(run) for run in runs_data)
                padded_runs = []

                for reward_list in runs_data:
                    if len(reward_list) < max_len:
                        # Pad with last value
                        padded = reward_list + [reward_list[-1]] * (max_len - len(reward_list))
                    else:
                        padded = reward_list
                    padded_runs.append(padded)

                # Convert to numpy array for easier manipulation
                runs_array = np.array(padded_runs)

                # Calculate mean and confidence interval
                mean_rewards = np.mean(runs_array, axis=0)
                sem_rewards = stats.sem(runs_array, axis=0)
                ci_lower = mean_rewards - 1.96 * sem_rewards
                ci_upper = mean_rewards + 1.96 * sem_rewards

                timesteps = np.arange(len(mean_rewards))

                # Plot individual runs with low alpha
                for run in runs_array:
                    ax.plot(timesteps, run, alpha=0.2, color="gray")

                # Plot mean with confidence interval
                ax.plot(timesteps, mean_rewards, color="blue", linewidth=2, label="Mean")
                ax.fill_between(timesteps, ci_lower, ci_upper, alpha=0.3, color="blue")

            # Set labels and title
            if i == 0:
                ax.set_title(env.replace("-v", " v"))
            if j == 0:
                ax.set_ylabel(f"{model}\nCumulative Reward")
            if i == n_models - 1:
                ax.set_xlabel("Step")

            ax.grid(True, alpha=0.3)

    plt.suptitle("Language Models as RL Agents (Raw Numerical Data)", fontsize=14)
    plt.tight_layout()

    # Save plot
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plt.savefig(f"llm_rl_results_{timestamp}.png", dpi=300, bbox_inches="tight")
    plt.savefig(f"llm_rl_results_{timestamp}.pdf", bbox_inches="tight")
    print(f"\nPlots saved as llm_rl_results_{timestamp}.png/.pdf")


async def main():
    """Run the full experiment."""
    print("Starting LLM RL Experiment with Raw Numerical Data")
    print("=" * 60)

    # Check API key
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable not set")
        return

    # Create logger for online logging
    logger = ExperimentLogger("llm_rl_experiment")
    print(f"\nLogging to: {logger.get_log_directory()}")

    # Create experiment parameters
    experiments = []
    for model in MODELS:
        for env_name in ENVIRONMENTS:
            for run_num in range(RUNS_PER_EXPERIMENT):
                experiments.append((model, env_name, run_num))

    print(f"\nRunning {len(experiments)} experiments...")
    print(f"(3 models x {len(ENVIRONMENTS)} environments x {RUNS_PER_EXPERIMENT} runs)")
    print(f"Concurrency limit: {MAX_CONCURRENT_EXPERIMENTS}")

    # Run experiments with limited concurrency
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXPERIMENTS)

    async def run_with_semaphore(model, env_name, run_num, exp_id):
        async with semaphore:
            return await run_experiment(model, env_name, run_num, exp_id, len(experiments), logger)

    tasks = [
        run_with_semaphore(model, env_name, run_num, i + 1) for i, (model, env_name, run_num) in enumerate(experiments)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out errors and collect successful runs
    all_runs: list[Run] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            model_idx = i // (len(ENVIRONMENTS) * RUNS_PER_EXPERIMENT)
            env_idx = (i // RUNS_PER_EXPERIMENT) % len(ENVIRONMENTS)
            run_idx = i % RUNS_PER_EXPERIMENT
            print(f"Error in {MODELS[model_idx]} on {ENVIRONMENTS[env_idx]} run {run_idx}: {result}")
        else:
            assert isinstance(result, Run)
            all_runs.append(result)

    # Save raw results with full trajectories
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = f"llm_rl_results_{timestamp}.json"
    trajectories_file = f"llm_rl_trajectories_{timestamp}.jsonl"

    # Convert results to serializable format
    serializable_results = []
    all_trajectories = []

    for run in all_runs:
        run_data: dict[str, Any] = {"model": run.model, "environment": run.environment, "episodes": []}
        for episode_idx, episode in enumerate(run.episodes):
            ep_data: dict[str, Any] = {
                "total_reward": episode.total_reward,
                "num_steps": len(episode.steps),
                "steps": [],
            }

            # Create trajectory record for detailed analysis
            trajectory: dict[str, Any] = {
                "model": run.model,
                "environment": run.environment,
                "episode_idx": episode_idx,
                "total_reward": episode.total_reward,
                "states": [],
                "actions": [],
                "rewards": [],
            }

            for step in episode.steps:
                # Full step data for main results
                ep_data["steps"].append(
                    {
                        "state": step.state.tolist() if isinstance(step.state, np.ndarray) else step.state,
                        "action": step.action,
                        "reward": step.reward,
                        "done": step.done,
                        "truncated": step.truncated,
                    }
                )

                # Trajectory data for analysis
                trajectory["states"].append(step.state.tolist() if isinstance(step.state, np.ndarray) else step.state)
                trajectory["actions"].append(step.action)
                trajectory["rewards"].append(step.reward)

            run_data["episodes"].append(ep_data)
            all_trajectories.append(trajectory)

        serializable_results.append(run_data)

    # Save main results
    async with aiofiles.open(results_file, "w") as f:
        await f.write(json.dumps(serializable_results, indent=2))
    print(f"\nRaw results saved to {results_file}")

    # Save trajectories in JSONL format for easier analysis
    async with aiofiles.open(trajectories_file, "w") as f:
        for trajectory in all_trajectories:
            await f.write(json.dumps(trajectory) + "\n")
    print(f"Trajectories saved to {trajectories_file}")

    # Save logger summary
    await logger.log_summary(all_runs)

    # Plot results
    plot_results(all_runs)

    # Save plots to log directory too
    plot_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plt.savefig(logger.log_dir / f"llm_rl_results_{plot_timestamp}.png", dpi=300, bbox_inches="tight")
    plt.savefig(logger.log_dir / f"llm_rl_results_{plot_timestamp}.pdf", bbox_inches="tight")

    print(f"\nExperiment complete! Logs saved to: {logger.get_log_directory()}")


if __name__ == "__main__":
    asyncio.run(main())
