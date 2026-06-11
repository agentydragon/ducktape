#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "pyarrow", "numpy", "matplotlib"]
# ///
"""Plot diagnostic curves from a wordle_train.py run's completion parquets.

Reads /tmp/wordle_grpo_output/completions/completions_*.parquet (one per
optimizer step, 64 rollouts each) and writes:

- reward_heatmap.png   : reward distribution over training (heatmap of fractions)
- invalid_curve.png    : wrong-length vs unknown-word format errors per rollout
- calls_per_turn.png   : tool_calls per assistant turn (parallel vs sequential play)
- guess_diversity.png  : unique guess words / sequences per window (mode-collapse)

Run from the rl_finetune dir; outputs land alongside this script.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DEFAULT_OUT_DIR = Path(__file__).parent
RUN_DIR = "/tmp/wordle_grpo_output/completions"

_RE_TOOL_CALL = re.compile(r"<tool_call>")
_RE_GUESS_WORD = re.compile(r'"word":\s*"([^"]+)"')
# Two distinct invalid responses from WordleEnv.guess:
_RE_INVALID_LENGTH = re.compile(r"Invalid: must be exactly")  # wrong length / non-alpha
_RE_INVALID_WORD = re.compile(r"is not a recognized English word")  # unknown 5-letter token
_RE_ALREADY_OVER = re.compile(r"Game already over")
_RE_TURN_SPLIT = re.compile(r"\n(assistant|user)\n")


def _parse_rollout(text: str) -> dict:
    blocks = _RE_TURN_SPLIT.split("\nassistant\n" + text)
    calls_per_turn: list[int] = []
    for j in range(1, len(blocks), 2):
        if blocks[j] != "assistant":
            continue
        body = blocks[j + 1] if j + 1 < len(blocks) else ""
        n = len(_RE_TOOL_CALL.findall(body))
        if n:
            calls_per_turn.append(n)
    return {
        "guesses": _RE_GUESS_WORD.findall(text),
        "n_invalid_length": len(_RE_INVALID_LENGTH.findall(text)),
        "n_invalid_word": len(_RE_INVALID_WORD.findall(text)),
        "n_already_over": len(_RE_ALREADY_OVER.findall(text)),
        "calls_per_turn": calls_per_turn,
        "n_assistant_turns_with_calls": len(calls_per_turn),
    }


def load_rollouts(run_dir: str = RUN_DIR) -> pd.DataFrame:
    files = sorted(Path(run_dir).glob("completions_*.parquet"))
    print(f"loading {len(files)} parquets...")
    parts = [pd.read_parquet(f, columns=["step", "reward_func", "completion"]) for f in files]
    df = pd.concat(parts, ignore_index=True)
    feats = df["completion"].map(_parse_rollout).apply(pd.Series)
    df = pd.concat([df.drop(columns=["completion"]), feats], axis=1)
    print(f"parsed {len(df)} rollouts, step range {df.step.min()}-{df.step.max()}")
    return df


def _window(df: pd.DataFrame, win: int) -> pd.DataFrame:
    df = df.copy()
    df["bucket"] = (df["step"] - 1) // win
    df["step_mid"] = df["bucket"] * win + win // 2
    return df


def plot_reward_heatmap(df: pd.DataFrame, win: int, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    """Heatmap of reward distribution over training. X=step bucket, Y=reward bucket,
    color=fraction of rollouts in that bucket. Mean overlaid as a white line."""
    df = _window(df, win)
    # Reward is quantized to 0.0, 0.1, ..., 1.0 (greens + 0.5*yellows over 5 letters,
    # plus 1.0 for wins). Bin to 11 fixed levels.
    bin_edges = np.arange(-0.05, 1.06, 0.1)
    bin_centers = np.arange(0.0, 1.01, 0.1)
    buckets = sorted(df["bucket"].unique())
    step_mids = np.array([df[df["bucket"] == b]["step_mid"].iloc[0] for b in buckets])
    hist = np.zeros((len(bin_centers), len(buckets)))
    means = np.zeros(len(buckets))
    for i, b in enumerate(buckets):
        rewards = df.loc[df["bucket"] == b, "reward_func"].to_numpy()
        counts, _ = np.histogram(rewards, bins=bin_edges)
        hist[:, i] = counts / counts.sum() if counts.sum() else 0.0
        means[i] = rewards.mean()

    fig, ax = plt.subplots(figsize=(11, 6))
    # Use pcolormesh so cells line up with their step-window x-extent.
    edges_x = np.concatenate([[step_mids[0] - win / 2], step_mids + win / 2])
    edges_y = bin_edges
    pcm = ax.pcolormesh(edges_x, edges_y, hist, cmap="viridis", vmin=0, vmax=hist.max())
    cb = fig.colorbar(pcm, ax=ax)
    cb.set_label("fraction of rollouts in window")
    # Mean overlay
    ax.plot(step_mids, means, color="white", lw=2.0, label="mean reward")
    ax.set_xlabel("step")
    ax.set_ylabel("reward")
    ax.set_yticks(bin_centers)
    ax.set_title(f"Wordle GRPO reward distribution — {win}-step windows ({len(df)} rollouts)")
    ax.legend(loc="upper left")
    out = out_dir / "reward_heatmap.png"
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def plot_invalid(df: pd.DataFrame, win: int, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    df = _window(df, win)
    agg = (
        df.groupby(["bucket", "step_mid"])[["n_invalid_length", "n_invalid_word", "n_already_over"]]
        .mean()
        .reset_index()
    )
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(
        agg["step_mid"],
        agg["n_invalid_length"],
        color="C3",
        lw=2,
        label='wrong-length / non-alpha ("Invalid: must be exactly...")',
    )
    ax.plot(
        agg["step_mid"],
        agg["n_invalid_word"],
        color="C1",
        lw=2,
        label='unknown 5-letter token ("X is not a recognized English word")',
    )
    ax.plot(
        agg["step_mid"], agg["n_already_over"], color="C2", lw=2, label='post-game-over guesses ("Game already over")'
    )
    ax.set_xlabel("step")
    ax.set_ylabel("count per rollout (mean over window)")
    ax.set_title(f"Wordle GRPO format errors — {win}-step windows")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    out = out_dir / "invalid_curve.png"
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def plot_calls_per_turn(df: pd.DataFrame, win: int, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    df = _window(df, win)
    df["mean_calls_per_turn"] = df["calls_per_turn"].map(lambda xs: float(np.mean(xs)) if xs else 0.0)
    df["max_calls_per_turn"] = df["calls_per_turn"].map(lambda xs: max(xs) if xs else 0)
    agg = (
        df.groupby(["bucket", "step_mid"])
        .agg(
            mean_calls=("mean_calls_per_turn", "mean"),
            p90_calls=("mean_calls_per_turn", lambda x: np.percentile(x, 90)),
            mean_max=("max_calls_per_turn", "mean"),
            n_turns=("n_assistant_turns_with_calls", "mean"),
        )
        .reset_index()
    )
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    x = agg["step_mid"]
    ax1.plot(x, agg["mean_calls"], color="C3", lw=2, label="mean")
    ax1.plot(x, agg["p90_calls"], color="C3", lw=1, ls="--", label="p90")
    ax1.plot(x, agg["mean_max"], color="C0", lw=2, label="max-in-rollout (mean)")
    ax1.set_xlabel("step")
    ax1.set_ylabel("tool_calls per assistant turn")
    ax1.set_title("Calls per assistant turn")
    ax1.grid(True, alpha=0.3)
    ax1.legend()
    ax2.plot(x, agg["n_turns"], color="C2", lw=2)
    ax2.set_xlabel("step")
    ax2.set_ylabel("assistant turns with tool_calls / rollout (mean)")
    ax2.set_title("Number of multi-turn rounds per rollout")
    ax2.grid(True, alpha=0.3)
    fig.suptitle(
        f"Wordle GRPO turn structure — {win}-step windows\n"
        "(6 calls in 1 turn = parallel-batch play; 1 call x 6 turns = sequential play)"
    )
    out = out_dir / "calls_per_turn.png"
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def plot_diversity(df: pd.DataFrame, win: int, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    df = _window(df, win)
    # Diversity counts (unique words, unique sequences, TTR, top1_frac) all scale with
    # sample size, so a partially-completed final window biases all of them downward.
    # Drop the last bucket if its step range isn't fully covered.
    last_b = df["bucket"].max()
    if df.loc[df["bucket"] == last_b, "step"].max() < (last_b + 1) * win:
        df = df[df["bucket"] != last_b]
    rows = []
    for (_, mid), g in df.groupby(["bucket", "step_mid"]):
        all_guesses: list[str] = [w for gs in g["guesses"] for w in gs]
        n_total = len(all_guesses)
        n_unique = len(set(all_guesses))
        ttr = n_unique / n_total if n_total else 0.0
        top1_frac = Counter(all_guesses).most_common(1)[0][1] / n_total if n_total else 0.0
        seqs = {tuple(gs) for gs in g["guesses"]}
        rows.append(
            {
                "step_mid": mid,
                "unique_words": n_unique,
                "total_guesses": n_total,
                "ttr": ttr,
                "top1_frac": top1_frac,
                "unique_sequences": len(seqs),
                "n_rollouts": len(g),
            }
        )
    out_df = pd.DataFrame(rows).sort_values("step_mid")
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    ax1.plot(out_df["step_mid"], out_df["unique_words"], color="C0", lw=2, label="unique words")
    ax1.plot(out_df["step_mid"], out_df["unique_sequences"], color="C2", lw=2, label="unique 6-guess sequences")
    ax1.set_xlabel("step")
    ax1.set_ylabel("count per window")
    ax1.set_title("Vocabulary / sequence diversity per window")
    ax1.grid(True, alpha=0.3)
    ax1.legend()
    ax2.plot(out_df["step_mid"], out_df["ttr"], color="C0", lw=2, label="type-token ratio (uniq / total)")
    ax2.plot(out_df["step_mid"], out_df["top1_frac"], color="C3", lw=2, label="frac of guesses = most common word")
    ax2.set_xlabel("step")
    ax2.set_ylabel("ratio")
    ax2.set_title("Mode-collapse signals")
    ax2.grid(True, alpha=0.3)
    ax2.legend()
    ax2.set_ylim(0, 1)
    fig.suptitle(f"Wordle GRPO guess diversity — {win}-step windows")
    out = out_dir / "guess_diversity.png"
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", default=RUN_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--window", type=int, default=100)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    df = load_rollouts(args.run_dir)
    print("plot:", plot_reward_heatmap(df, args.window, args.out_dir))
    print("plot:", plot_invalid(df, args.window, args.out_dir))
    print("plot:", plot_calls_per_turn(df, args.window, args.out_dir))
    print("plot:", plot_diversity(df, args.window, args.out_dir))

    # Quick text summary at start vs end.
    early = df[df.step <= 100]
    late = df[df.step > df.step.max() - 100]

    def summary(label: str, sub: pd.DataFrame) -> None:
        if sub.empty:
            return
        all_guesses = [w for gs in sub["guesses"] for w in gs]
        seqs = {tuple(gs) for gs in sub["guesses"]}
        mean_calls = float(np.mean([np.mean(xs) for xs in sub["calls_per_turn"] if xs])) if not sub.empty else 0
        print(
            f"{label:>5}: "
            f"reward_mean={sub.reward_func.mean():.3f}  "
            f"inv_len={sub.n_invalid_length.mean():.2f}  "
            f"inv_word={sub.n_invalid_word.mean():.2f}  "
            f"already_over={sub.n_already_over.mean():.2f}  "
            f"unique_words={len(set(all_guesses))}/{len(all_guesses)}  "
            f"unique_seqs={len(seqs)}/{len(sub)}  "
            f"mean_calls/turn={mean_calls:.2f}"
        )

    print()
    summary("early", early)
    summary("late", late)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
