"""Plot the recorded filesystem and Forgejo samples without rerunning benchmarks."""

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import median

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.lines import Line2D

matplotlib.use("Agg")

ARMS = {
    "local-path-ovh-hdd": ("Local-path HDD", "#596675"),
    "seaweedfs-ovh": ("SeaweedFS HDD", "#bd6600"),
    "piraeus-file-thin-one": ("Piraeus · 1 copy", "#2368b5"),
    "piraeus-file-thin-two-protocol-c": ("Piraeus · 2 copies (C)", "#00866a"),
}
DIRECT_PANELS = (
    ("fsync_200", "200 × 4 KiB fsync writes"),
    ("git_commit_50", "50 tiny Git commits"),
    ("git_clone", "Local Git clone"),
)
FORGEJO_PANELS = (
    ("version", "Version lookup"),
    ("contents_read", "Content read"),
    ("contents_write", "Content write"),
    ("git_push", "Tiny Git push"),
)

type Samples = dict[tuple[str, str], list[float]]


def read_samples(paths: list[Path], operations: set[str], count: int, *, forgejo: bool) -> Samples:
    samples: Samples = defaultdict(list)
    for path in paths:
        with path.open(newline="") as stream:
            for row in csv.DictReader(stream):
                arm, operation = row["target"], row["operation"]
                seconds = float(row["seconds"])
                if arm not in ARMS or operation not in operations or not math.isfinite(seconds) or seconds < 0:
                    raise ValueError(f"Invalid measurement in {path}: {row}")
                if forgejo and row["http_status"] not in {"200", "201"}:
                    raise ValueError(f"Unsuccessful request in {path}: {row}")
                samples[arm, operation].append(seconds)
    for arm in ARMS:
        for operation in operations:
            values = samples.get((arm, operation), [])
            if forgejo and arm == "seaweedfs-ovh" and not values:
                continue
            if len(values) != count:
                raise ValueError(f"Expected {count} samples for {arm}/{operation}, got {len(values)}")
    return samples


def panel(ax: Axes, samples: Samples, operation: str, title: str, *, labels: bool) -> None:
    largest = max(value for (arm, op), values in samples.items() if op == operation for value in values)
    for y, (arm, (_label, color)) in enumerate(ARMS.items()):
        values = sorted(samples.get((arm, operation), []))
        if not values:
            ax.text(0.04, y, "No samples (init EIO)", transform=ax.get_yaxis_transform(), va="center", color="#815500")
            continue
        offsets = [y + 0.26 * (i / (len(values) - 1) - 0.5) for i in range(len(values))]
        ax.scatter(values, offsets, color=color, alpha=0.4, s=17, edgecolors="none", zorder=2, clip_on=False)
        middle = median(values)
        ax.scatter(
            [middle], [y], marker="D", color=color, edgecolors="white", linewidths=0.8, s=48, zorder=3, clip_on=False
        )
        ax.text(0.99, y, f"{middle:.3f}", transform=ax.get_yaxis_transform(), ha="right", va="center", fontsize=9)
    ax.set(title=title, xlim=(0, largest * 1.30), ylim=(3.55, -0.55), xlabel="Seconds · lower is faster")
    ax.set_yticks(range(len(ARMS)), [label for label, _ in ARMS.values()])
    ax.tick_params(axis="y", length=0, pad=10, labelleft=labels)
    ax.grid(axis="x", color="#e1e5e9", linewidth=0.7)
    ax.set_axisbelow(True)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#b8c0c8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Destination SVG")
    parser.add_argument("--preview", type=Path, help="Optional PNG for visual inspection")
    args = parser.parse_args()
    direct = read_samples(
        [args.results_dir / "direct-filesystem.csv"], {op for op, _ in DIRECT_PANELS}, 5, forgejo=False
    )
    forgejo = read_samples(
        sorted(args.results_dir.glob("forgejo-*.csv")), {op for op, _ in FORGEJO_PANELS}, 20, forgejo=True
    )
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "svg.hashsalt": "piraeus-storage-bench"})
    fig = plt.figure(figsize=(17, 9), facecolor="white")
    grid = fig.add_gridspec(2, 4, left=0.15, right=0.97, top=0.79, bottom=0.16, hspace=0.85, wspace=0.40)
    fig.suptitle("Piraeus storage: local versus synchronous copies", x=0.15, y=0.97, ha="left", fontsize=21)
    fig.text(
        0.15,
        0.925,
        "2026-10-19 UTC · Single writer on ovh-ns102453 · Exploratory shared-cluster HDD run",
        color="#52606d",
    )
    fig.text(0.15, 0.845, "FILESYSTEM WORKLOADS  ·  5 samples per arm", weight="bold", fontsize=11)
    fig.text(0.15, 0.445, "FORGEJO WORKLOADS  ·  20 samples per successful arm", weight="bold", fontsize=11)
    for row, (samples, panels) in enumerate(((direct, DIRECT_PANELS), (forgejo, FORGEJO_PANELS))):
        for column, (operation, title) in enumerate(panels):
            panel(fig.add_subplot(grid[row, column]), samples, operation, title, labels=column == 0)
    notes = fig.add_subplot(grid[0, 3])
    notes.axis("off")
    notes.legend(
        handles=[
            Line2D([], [], marker="o", linestyle="none", color="#596675", alpha=0.4, label="Individual samples"),
            Line2D([], [], marker="D", linestyle="none", color="#596675", label="Median (value at right)"),
        ],
        loc="upper left",
        frameon=False,
    )
    notes.text(
        0.04,
        0.53,
        "Each panel has its own linear scale.\n\nSeaweedFS Forgejo init failed;\nmissing results are never zero.",
        va="top",
        linespacing=1.6,
        fontsize=10,
    )
    fig.text(
        0.15,
        0.075,
        "Piraeus: ext4 over FILE_THIN on XFS, explicit 1 MiB discard; local-path: XFS; SeaweedFS: FUSE.",
        fontsize=10,
    )
    fig.text(
        0.15,
        0.035,
        "Git/API timings do not establish crash durability. Local clones may use cache/hardlinks. See README for setup failures and limits.",
        fontsize=9,
        color="#52606d",
    )
    fig.savefig(args.output, format="svg", metadata={"Date": None})
    # Matplotlib's multiline path data contains trailing spaces; keep generated
    # output consistent with the repository's whitespace hook.
    args.output.write_text("".join(f"{line.rstrip()}\n" for line in args.output.read_text().splitlines()))
    if args.preview:
        fig.savefig(args.preview, dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    main()
