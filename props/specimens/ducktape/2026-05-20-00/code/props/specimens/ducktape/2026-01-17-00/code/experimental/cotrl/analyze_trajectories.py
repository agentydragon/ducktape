#!/usr/bin/env python3
"""TEMPORARY ONE-OFF to analyze LLM RL trajectories.

Can delete after: Trajectory analysis complete
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def load_trajectories(filename):
    """Load trajectories from JSONL file."""
    trajectories = []
    with Path(filename).open(encoding="utf-8") as f:
        for line in f:
            trajectories.append(json.loads(line))
    return trajectories


def analyze_action_distribution(trajectories):
    """Analyze action distribution by model and environment."""
    action_counts: defaultdict[str, defaultdict[str, defaultdict[int, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )

    for traj in trajectories:
        model = traj["model"]
        env = traj["environment"]
        for action in traj["actions"]:
            action_counts[model][env][action] += 1

    return action_counts


def plot_action_heatmap(action_counts):
    """Plot heatmap of action distributions."""
    models = sorted(action_counts.keys())
    envs = sorted(next(iter(action_counts.values())).keys())

    fig, axes = plt.subplots(len(models), len(envs), figsize=(2.5 * len(envs), 2.5 * len(models)))
    if len(models) == 1:
        axes = axes.reshape(1, -1)
    if len(envs) == 1:
        axes = axes.reshape(-1, 1)

    for i, model in enumerate(models):
        for j, env in enumerate(envs):
            ax = axes[i, j]

            # Get action distribution
            actions = action_counts[model][env]
            if actions:
                max_action = max(actions.keys())
                action_dist = [actions.get(a, 0) for a in range(max_action + 1)]
                total = sum(action_dist)
                action_probs = [a / total for a in action_dist]

                # Plot as bar chart
                ax.bar(range(len(action_probs)), action_probs)
                ax.set_ylim(0, 1)

            if i == 0:
                ax.set_title(env.split("-")[0], fontsize=10)
            if j == 0:
                ax.set_ylabel(f"{model}\nP(action)", fontsize=9)
            if i == len(models) - 1:
                ax.set_xlabel("Action", fontsize=9)

            ax.set_xticks(range(len(action_probs)))
            ax.grid(True, alpha=0.3)

    plt.suptitle("Action Distribution by Model and Environment")
    plt.tight_layout()
    return fig


def analyze_state_visits(trajectories):
    """For discrete state envs, analyze state visitation."""
    discrete_envs = ["FrozenLake-v1", "CliffWalking-v1"]
    state_visits: defaultdict[str, defaultdict[str, defaultdict[int, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )

    for traj in trajectories:
        if traj["environment"] not in discrete_envs:
            continue

        model = traj["model"]
        env = traj["environment"]
        for state in traj["states"]:
            state_val = state[0] if isinstance(state, list) else state
            state_visits[model][env][int(state_val)] += 1

    return state_visits


def main():
    """Analyze trajectory data."""
    if len(sys.argv) < 2:
        print("Usage: python analyze_trajectories.py llm_rl_trajectories_TIMESTAMP.jsonl")
        sys.exit(1)

    filename = sys.argv[1]
    print(f"Loading trajectories from {filename}...")
    trajectories = load_trajectories(filename)
    print(f"Loaded {len(trajectories)} trajectories")

    # Basic statistics
    by_model_env = defaultdict(list)
    for traj in trajectories:
        key = (traj["model"], traj["environment"])
        by_model_env[key].append(traj["total_reward"])

    print("\n=== Average Rewards ===")
    for (model, env), rewards in sorted(by_model_env.items()):
        avg_reward = np.mean(rewards)
        std_reward = np.std(rewards)
        print(f"{model} on {env}: {avg_reward:.2f} ± {std_reward:.2f}")

    # Analyze action distributions
    print("\n=== Action Distribution Analysis ===")
    action_counts = analyze_action_distribution(trajectories)

    # Print most common actions
    for model in sorted(action_counts.keys()):
        print(f"\n{model}:")
        for env in sorted(action_counts[model].keys()):
            actions = action_counts[model][env]
            total = sum(actions.values())
            most_common = max(actions.items(), key=lambda x: x[1])
            print(f"  {env}: Action {most_common[0]} ({most_common[1] / total:.1%} of {total} total)")

    # Plot action distributions
    fig = plot_action_heatmap(action_counts)
    output_path = filename.replace(".jsonl", "_action_dist.png")
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    print(f"\nAction distribution plot saved to {output_path}")

    # Analyze state visits for discrete envs
    print("\n=== State Visitation (Discrete Envs) ===")
    state_visits = analyze_state_visits(trajectories)
    for model in sorted(state_visits.keys()):
        for env in sorted(state_visits[model].keys()):
            visits = state_visits[model][env]
            n_states = len(visits)
            most_visited = max(visits.items(), key=lambda x: x[1])
            print(
                f"{model} on {env}: Visited {n_states} unique states, "
                f"most common: state {most_visited[0]} ({most_visited[1]} times)"
            )


if __name__ == "__main__":
    main()
