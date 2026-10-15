"""Plot published AA indices against measured rugged decode speed (no network)."""

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

matplotlib.use("Agg")


@dataclass(frozen=True)
class Measurement:
    name: str
    slug: str
    speed: float
    low: float
    high: float
    context: str
    offset: tuple[int, int]


# Source: ../rugged_local_llm.md, Repeated speed probes, AC short-prompt column.
# Three repetitions, ~500 input tokens, 256 output tokens. Ranges are observed,
# not confidence intervals. Context labels come from Context capacity, separately.
AC = (
    Measurement(
        "Qwen3 4B Instruct 2507", "qwen3-4b-2507-instruct", 11.9, 9.4, 14.1, "~21k input / 32k allocation", (16, 48)
    ),
    Measurement("Gemma 4 E4B", "gemma-4-e4b-non-reasoning", 14.0, 11.1, 15.7, "~21k input / 32k allocation", (16, 5)),
    Measurement(
        "Qwen2.5-Coder 7B", "qwen2-5-coder-7b-instruct", 5.4, 5.2, 5.4, "~21k input / 32k allocation", (-110, -16)
    ),
    Measurement("Qwen3 8B", "qwen3-8b-instruct", 4.7, 4.3, 4.8, "~21k input / 32k allocation", (16, -18)),
    Measurement(
        "Gemma 4 E2B QAT",
        "gemma-4-e2b-non-reasoning",
        34.0,
        33.2,
        34.4,
        "124,005 input / 131,072 allocation; recall 1/3",
        (16, 0),
    ),
)


# Same dated AA response as the rugged scores; color shades distinguish references.
REFERENCES = (
    ("gpt-35-turbo", "#9bc9b4"),
    ("gpt-4o-mini", "#69ae91"),
    ("gpt-4", "#3d9274"),
    ("gpt-4o-2024-05-13", "#24735a"),
    ("gpt-4-1-mini", "#125640"),
    ("o3-mini-high", "#073b2b"),
    ("gpt-5-1-non-reasoning", "#005c46"),
    ("claude-3-haiku", "#e4a08a"),
    ("claude-3-sonnet", "#c56d52"),
    ("claude-35-sonnet-june-24", "#923e2e"),
    ("claude-3-7-sonnet", "#702b20"),
)


@dataclass(frozen=True)
class LlamaMeasurement:
    name: str
    slug: str
    speed: float
    context: str
    offset: tuple[int, int]
    long_input: bool


# Direct-chat smoke requests, one observation each, q4_0 KV, 139,264 allocation.
# Short points use the tool-result response, not an average of different tasks.
LLAMA = (
    LlamaMeasurement(
        "Qwen3.5 4B Q4_K_M",
        "qwen3-5-4b-non-reasoning",
        25.755646523068208,
        "337 input; long-context check pending",
        (15, 12),
        False,
    ),
    LlamaMeasurement(
        "Qwen3.5 9B Q4_K_M",
        "qwen3-5-9b-non-reasoning",
        15.66047055798909,
        "337 input; short tool response",
        (-145, 22),
        False,
    ),
    LlamaMeasurement(
        "Qwen3.5 9B Q4_K_M",
        "qwen3-5-9b-non-reasoning",
        9.111569415265034,
        "63,907 input; recall 3/3; prefill 19.02 min",
        (-210, 3),
        True,
    ),
    LlamaMeasurement(
        "Qwen3.5 9B Q4_K_M",
        "qwen3-5-9b-non-reasoning",
        6.203453223815344,
        "131,907 input; recall 3/3; prefill 66.54 min",
        (-160, -32),
        True,
    ),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", type=Path, required=True, help="AA 2026-10-14 CSV")
    parser.add_argument("--output", type=Path, required=True, help="Output SVG or PNG")
    args = parser.parse_args()
    with args.scores.open() as stream:
        scores = {row["slug"]: row for row in csv.DictReader(stream)}
    plt.rcParams.update({"font.size": 10, "svg.hashsalt": "rugged-intelligence-speed"})
    fig, ax = plt.subplots(figsize=(18, 9))
    reference_handles = []
    for slug, color in REFERENCES:
        row = scores[slug]
        x = float(row["intelligence_index"])
        estimated = row["intelligence_index_is_estimated"] == "true"
        style: Literal["--", ":"] = "--" if row["creator"] == "OpenAI" else ":"
        ax.axvline(x, color=color, linestyle=style, linewidth=1.3, alpha=0.8, zorder=0)
        # Put close pairs on opposite sides of their lines; keep names above data.
        label_left = slug in {"gpt-35-turbo", "gpt-4o-mini"}
        ax.annotate(
            row["model"],
            xy=(x, 0.98),
            xycoords=ax.get_xaxis_transform(),
            xytext=(-4 if label_left else 4, 0),
            textcoords="offset points",
            rotation=90,
            ha="right" if label_left else "left",
            va="top",
            fontsize=9,
            color=color,
        )
        suffix = " (est.)" if estimated else ""
        reference_handles.append(
            Line2D([], [], color=color, linestyle=style, label=f"{row['model']} — {x:.2f}{suffix}")
        )
    reference_legend = ax.legend(
        handles=reference_handles,
        loc="upper left",
        bbox_to_anchor=(1.02, 1),
        title="External intelligence references\nOpenAI dashed · Anthropic dotted",
        fontsize=9,
        title_fontsize=10,
        frameon=False,
    )
    ax.add_artist(reference_legend)
    for point in AC:
        row = scores[point.slug]
        estimated = row["intelligence_index_is_estimated"] == "true"
        x = float(row["intelligence_index"])
        ax.errorbar(
            x,
            point.speed,
            yerr=[[point.speed - point.low], [point.high - point.speed]],
            fmt="o",
            color="#2266aa",
            markerfacecolor="white" if estimated else "#2266aa",
            capsize=4,
            markersize=8,
        )
        ax.annotate(
            f"{point.name}\n{point.context}",
            (x, point.speed),
            xytext=point.offset,
            textcoords="offset points",
            fontsize=9,
            arrowprops={"arrowstyle": "-", "color": "#999999"},
        )
    # Single long-input request, not comparable to the AC short-prompt cohort.
    row = scores["qwen3-5-0-8b-non-reasoning"]
    x = float(row["intelligence_index"])
    speed = 50 / 3.50  # Documented 50 output tokens in 3.50 seconds.
    ax.scatter([x], [speed], marker="D", facecolors="white", edgecolors="#bd6500", s=65)
    ax.annotate(
        "Qwen3.5 0.8B Q8 (single long-input run)\n134,993 input / 163,840 allocation; recall 3/3",
        (x, speed),
        xytext=(0, 80),
        textcoords="offset points",
        fontsize=9,
        arrowprops={"arrowstyle": "-", "color": "#999999"},
    )
    for observation in LLAMA:
        row = scores[observation.slug]
        x = float(row["intelligence_index"])
        color = "#8a3994" if observation.long_input else "#167b80"
        ax.scatter(
            [x],
            [observation.speed],
            marker="D" if observation.long_input else "s",
            facecolors="white" if row["intelligence_index_is_estimated"] == "true" else color,
            edgecolors=color,
            s=65,
        )
        ax.annotate(
            f"{observation.name}\n{observation.context}",
            (x, observation.speed),
            xytext=observation.offset,
            textcoords="offset points",
            fontsize=9,
            arrowprops={"arrowstyle": "-", "color": "#999999"},
        )
    ax.set(
        xlabel="Artificial Analysis Intelligence Index v4.3.2 (rugged models: non-reasoning; references: as labeled)",
        ylabel="Measured decode tokens/second on rugged",
        xlim=(4.8, 16.0),
        ylim=(0, 60),
    )
    ax.set_title("Rugged: intelligence × decode speed, with demonstrated context", fontsize=15, pad=18)
    ax.grid(alpha=0.22)
    ax.legend(
        handles=[
            Line2D(
                [],
                [],
                marker="o",
                markerfacecolor="white",
                color="#2266aa",
                linestyle="none",
                label="Ollama Vulkan, AC short prompts\nMedian and observed range, n=3",
            ),
            Line2D(
                [],
                [],
                marker="D",
                markerfacecolor="white",
                color="#bd6500",
                linestyle="none",
                label="Ollama Vulkan, one long prompt\n34.2 minutes prefill, AC, n=1",
            ),
            Line2D(
                [],
                [],
                marker="s",
                markerfacecolor="white",
                color="#167b80",
                linestyle="none",
                label="llama.cpp Vulkan, short tool response\n17-25 output tokens, AC, n=1 each",
            ),
            Line2D(
                [],
                [],
                marker="D",
                markerfacecolor="white",
                color="#8a3994",
                linestyle="none",
                label="llama.cpp Vulkan, long retrieval\n27 output tokens, AC, n=1 each",
            ),
        ],
        loc="upper left",
        bbox_to_anchor=(1.02, 0.40),
        fontsize=9,
    )
    fig.text(
        0.08,
        0.18,
        "Vertical lines show external intelligence only, not local throughput or equivalent coding capability.\n"
        "Hollow markers: AA index is estimated. Scores were not reproduced locally or matched to our quantization.\n"
        "Ollama context labels describe separate tests; llama.cpp labels give the actual prompt sizes (139,264 allocation).\n"
        "llama.cpp uses Q4_K_M weights / q4_0 KV; tiny output samples are smoke checks, not stable throughput estimates.\n"
        "No Pareto frontier: power, prompt length, cache settings and workload conditions are not controlled across all points.",
        fontsize=9,
        va="top",
    )
    fig.text(
        0.08,
        0.07,
        "Not plotted: NPU TinyLlama ~38.65 tok/s and Qwen2.5-Coder 1.5B 45.76 tok/s lack matching AA rows.\n"
        "Colibri Qwen3-Coder 30B-A3B lacks decode-phase timing. Qwen3.5 35B-A3B has no completed measurement yet.\n"
        "Sources: rugged_local_llm.md (2026-10-13/14 runs); docs/artificial_analysis/cited_models_2026_10_14.csv.",
        fontsize=9,
        va="top",
    )
    fig.subplots_adjust(left=0.08, right=0.72, top=0.90, bottom=0.29)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, metadata={"Date": None})
    plt.close(fig)
    if args.output.suffix.lower() == ".svg":
        args.output.write_text("\n".join(line.rstrip() for line in args.output.read_text().splitlines()) + "\n")


if __name__ == "__main__":
    main()
