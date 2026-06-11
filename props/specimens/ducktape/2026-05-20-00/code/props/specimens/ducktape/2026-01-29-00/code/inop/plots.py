"""Score evolution tracking, plotting functionality, and rollout analysis."""

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import pandas as pd
import tiktoken
from plotnine import (
    aes,
    element_text,
    facet_wrap,
    geom_errorbar,
    geom_line,
    geom_point,
    ggplot,
    labs,
    position_dodge,
    theme,
    theme_minimal,
)
from pydantic import BaseModel

from inop.io.logging_utils import DualOutputLogging

# Force non-interactive backend to avoid UI popups (prod/tests)

matplotlib.use("Agg", force=True)


logger = DualOutputLogging.get_logger()


class Stats(BaseModel):
    mean: float
    stdev: float
    min: float
    max: float
    count: int


class IterationSummary(BaseModel):
    iteration: int
    overall: Stats
    facets: dict[str, Stats]
    timestamp: str


class PlotDataPoint(BaseModel):
    iteration: int
    facet: str
    mean: float
    stdev: float
    ci_lower: float
    ci_upper: float
    count: int


def create_plot_data_point(iter_data: IterationSummary, facet_name: str) -> PlotDataPoint:
    """Create a plot data point for a given iteration and facet."""
    stats = iter_data.overall if facet_name == "overall" else iter_data.facets[facet_name]

    # Calculate 69% confidence interval (approximately 1 standard error)
    mean = stats.mean
    count = stats.count
    if count > 1:
        std_err = stats.stdev / (count**0.5)
        ci_lower = mean - std_err
        ci_upper = mean + std_err
    else:
        ci_lower = ci_upper = mean

    return PlotDataPoint(
        iteration=iter_data.iteration,
        facet=facet_name,
        mean=mean,
        stdev=stats.stdev,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        count=count,
    )


class ScoreStatistics:
    """Calculates statistics from score data."""

    @staticmethod
    def safe_stats(scores: list[float]) -> dict[str, float]:
        """Calculate statistics safely, handling empty lists."""
        if not scores:
            return {"mean": 0, "stdev": 0, "min": 0, "max": 0, "count": 0}
        return {
            "mean": statistics.mean(scores),
            "stdev": statistics.stdev(scores) if len(scores) > 1 else 0,
            "min": min(scores),
            "max": max(scores),
            "count": len(scores),
        }

    @staticmethod
    def extract_scores(graded_codes) -> tuple[list[float], dict[str, list[float]]]:
        """Extract overall and facet scores from graded codes."""
        overall_scores = [gc.grade.overall_score for gc in graded_codes]

        facet_scores = {}
        if graded_codes:
            # Get all facet names from first result
            facet_names = list(graded_codes[0].grade.axes.keys())
            for facet_name in facet_names:
                facet_scores[facet_name] = [gc.grade.axes[facet_name].score for gc in graded_codes]

        return overall_scores, facet_scores


class ScoreEvolutionPlotter:
    """Handles plotting of score evolution data."""

    def __init__(self, iterations_data: list[IterationSummary]):
        self.iterations_data = iterations_data

    def generate_plots(self, run_dir: Path) -> tuple[Path, Path]:
        """Generate score evolution plots using plotnine."""

        plot_data: list[PlotDataPoint] = []
        for iter_data in self.iterations_data:
            plot_data.append(create_plot_data_point(iter_data, "overall"))

        if self.iterations_data and self.iterations_data[0].facets:
            for facet_name in self.iterations_data[0].facets:
                for iter_data in self.iterations_data:
                    plot_data.append(create_plot_data_point(iter_data, facet_name))

        plot_df = pd.DataFrame([p.model_dump() for p in plot_data])

        combined_plot = self._create_combined_plot(plot_df)
        faceted_plot = self._create_faceted_plot(plot_df)

        combined_plot_path = run_dir / "score_evolution.png"
        faceted_plot_path = run_dir / "score_evolution_faceted.png"

        combined_plot.save(combined_plot_path, width=12, height=8, dpi=300)
        faceted_plot.save(faceted_plot_path, width=16, height=12, dpi=300)

        return combined_plot_path, faceted_plot_path

    def _create_combined_plot(self, plot_df: pd.DataFrame) -> ggplot:
        """Create combined plot with position dodging."""

        dodge_width = 0.3

        df_overall = plot_df[plot_df["facet"] == "overall"]
        df_other = plot_df[plot_df["facet"] != "overall"]

        plot: ggplot = ggplot()
        plot = plot + geom_line(mapping=aes(x="iteration", y="mean", color="facet"), data=df_other, size=0.8, alpha=0.7)
        plot = plot + geom_point(
            mapping=aes(x="iteration", y="mean", color="facet"),
            data=df_other,
            size=1.5,
            position=position_dodge(width=dodge_width),
            alpha=0.7,
        )
        plot = plot + geom_errorbar(
            mapping=aes(x="iteration", ymin="ci_lower", ymax="ci_upper", color="facet"),
            data=df_other,
            width=0.1,
            position=position_dodge(width=dodge_width),
            alpha=0.7,
        )
        plot = plot + geom_line(mapping=aes(x="iteration", y="mean"), data=df_overall, color="black", size=2.5)
        plot = plot + geom_point(
            mapping=aes(x="iteration", y="mean"),
            data=df_overall,
            color="black",
            size=3,
            position=position_dodge(width=dodge_width),
        )
        plot = plot + geom_errorbar(
            mapping=aes(x="iteration", ymin="ci_lower", ymax="ci_upper"),
            data=df_overall,
            width=0.1,
            position=position_dodge(width=dodge_width),
            color="black",
            size=1,
        )
        plot = plot + theme_minimal()
        plot = plot + labs(
            title="Score Evolution Across Iterations",
            x="Iteration",
            y="Score",
            color="Facet",
            caption="Error bars show 69% confidence interval of the mean",
        )
        return plot + theme(plot_title=element_text(size=14, ha="center"), legend_position="right")

    def _create_faceted_plot(self, plot_df: pd.DataFrame) -> ggplot:
        """Create faceted plot - separate subplot for each facet."""

        plot: ggplot = ggplot(plot_df, aes(x="iteration", y="mean"))
        plot = plot + geom_line(size=1, color="steelblue")
        plot = plot + geom_point(size=2, color="steelblue")
        plot = plot + geom_errorbar(mapping=aes(ymin="ci_lower", ymax="ci_upper"), width=0.1, color="steelblue")
        plot = plot + facet_wrap("facet", scales="free_y", ncol=2)
        plot = plot + theme_minimal()
        plot = plot + labs(
            title="Score Evolution Across Iterations by Facet",
            x="Iteration",
            y="Score",
            caption="Error bars show 69% confidence interval of the mean",
        )
        return plot + theme(
            plot_title=element_text(size=14, ha="center"),
            strip_text=element_text(size=10, margin={"t": 6, "b": 6}),
            axis_text_x=element_text(angle=0),
            panel_spacing=0.5,
            figure_size=(16, 12),
        )


class ScoreEvolutionReporter:
    """Builds textual reports describing score evolution."""

    def __init__(self, iterations_data: list[IterationSummary]):
        self.iterations_data = iterations_data

    def generate_report(self, run_dir: Path, log_path: Path, plot_paths: tuple[Path, Path] | None = None) -> str:
        """Generate final score evolution report."""
        if not self.iterations_data:
            return "No score data to report."

        report_parts = [
            "=== SCORE EVOLUTION REPORT ===",
            f"Total iterations: {len(self.iterations_data)}",
            f"Log files location: {log_path}",
            "",
            "Overall Score Evolution:",
        ]

        for iter_data in self.iterations_data:
            overall: Stats = iter_data.overall
            report_parts.append(
                f"  Iteration {iter_data.iteration:2d}: "
                f"{overall.mean:5.2f} ± {overall.stdev:4.2f} "
                f"(range: {overall.min:4.1f}-{overall.max:4.1f}, n={overall.count})"
            )

        if self.iterations_data and self.iterations_data[0].facets:
            report_parts.extend(["", "Facet Score Evolution:"])
            facet_names = list(self.iterations_data[0].facets.keys())

            for facet in facet_names:
                report_parts.append(f"  {facet}:")
                for iter_data in self.iterations_data:
                    facet_stats = iter_data.facets[facet]
                    report_parts.append(
                        f"    Iter {iter_data.iteration:2d}: {facet_stats.mean:5.2f} ± {facet_stats.stdev:4.2f}"
                    )

        if plot_paths:
            combined_path, faceted_path = plot_paths
            report_parts.extend(
                [
                    "",
                    "Score evolution plots saved to:",
                    f"  - Combined: {combined_path}",
                    f"  - Faceted: {faceted_path}",
                ]
            )

        report_parts.append("=" * 50)
        return "\n".join(report_parts)


class ScoreEvolutionTracker:
    """Tracks how scores evolve across optimization iterations."""

    def __init__(self):
        self.iterations_data = []  # List of iteration score summaries
        self._stats = ScoreStatistics()

    def add_iteration(self, iteration: int, graded_codes):
        """Add scores from an iteration."""
        overall_scores, facet_scores = self._stats.extract_scores(graded_codes)

        iteration_summary = IterationSummary(
            iteration=iteration,
            overall=Stats.model_validate(self._stats.safe_stats(overall_scores)),
            facets={
                name: Stats.model_validate(self._stats.safe_stats(scores)) for name, scores in facet_scores.items()
            },
            timestamp=datetime.now(UTC).isoformat(),
        )

        self.iterations_data.append(iteration_summary)

        logger.info(
            "Score evolution tracked",
            iteration=iteration,
            overall_mean=round(iteration_summary.overall.mean, 2),
            overall_stdev=round(iteration_summary.overall.stdev, 2),
            rollout_count=iteration_summary.overall.count,
        )

    def generate_report(self, run_dir: Path, log_path: Path) -> str:
        """Generate final score evolution report and plots."""
        reporter = ScoreEvolutionReporter(self.iterations_data)

        # Generate plots
        plot_paths = None
        try:
            plotter = ScoreEvolutionPlotter(self.iterations_data)
            plot_paths = plotter.generate_plots(run_dir)
        except Exception as e:
            logger.error("Failed to generate plots", error=str(e))
            # Plot generation failure is not critical for the core optimization process
            # so we continue but log the error prominently

        return reporter.generate_report(run_dir, log_path, plot_paths)


# Rollout analysis functions for token usage estimation


class TokenStats(BaseModel):
    min: int
    max: int
    avg: float
    median: float


class RolloutDetail(BaseModel):
    task_tokens: int
    code_tokens: int
    messages_tokens: int
    total_tokens: int
    task_id: str
    iteration: int


class AnalysisStats(BaseModel):
    total_rollouts: int
    token_stats: dict[str, TokenStats]
    rollout_details: list[RolloutDetail]


def analyze_rollout_logs(log_path: Path) -> AnalysisStats | None:
    """Analyze rollout logs to estimate token usage."""

    if not log_path.exists():
        logger.warning("Log file not found", log_path=str(log_path))
        return None

    # Initialize tiktoken encoder (using gpt-4o model for consistency)
    enc = tiktoken.encoding_for_model("gpt-4o")

    rollouts = []
    skipped_lines = 0

    with log_path.open() as f:
        for line_num, line in enumerate(f, 1):
            try:
                rollout = json.loads(line.strip())
                rollouts.append(rollout)
            except json.JSONDecodeError as e:
                skipped_lines += 1
                logger.warning("Skipped malformed JSON line", line_num=line_num, error=str(e))
                continue

    if skipped_lines > 0:
        logger.warning("Skipped malformed lines", count=skipped_lines)

    if not rollouts:
        logger.warning("No rollouts found in log file")
        return None

    # Analyze each rollout
    rollout_data: list[RolloutDetail] = []

    for rollout in rollouts:
        # Extract key fields
        task = rollout.get("task", "")
        code = rollout.get("code", "")
        messages = rollout.get("messages", [])

        # Calculate token counts
        task_tokens = len(enc.encode(task))
        code_tokens = len(enc.encode(code))

        # Messages token count (serialized)
        messages_str = json.dumps(messages)
        messages_tokens = len(enc.encode(messages_str))

        # Total rollout size
        total_tokens = task_tokens + code_tokens + messages_tokens

        rollout_data.append(
            RolloutDetail(
                task_tokens=task_tokens,
                code_tokens=code_tokens,
                messages_tokens=messages_tokens,
                total_tokens=total_tokens,
                task_id=str(rollout.get("agent_id", "unknown")),
                iteration=int(rollout.get("iteration", 1)),
            )
        )

    # Create DataFrame for efficient statistics computation
    rollout_df = pd.DataFrame(rollout_data)

    # Calculate statistics using pandas
    token_stats: dict[str, TokenStats] = {}
    for col in ["total_tokens", "code_tokens", "messages_tokens"]:
        token_stats[col] = TokenStats(
            min=int(rollout_df[col].min()),
            max=int(rollout_df[col].max()),
            avg=float(rollout_df[col].mean()),
            median=float(rollout_df[col].median()),
        )

    return AnalysisStats(total_rollouts=len(rollout_data), token_stats=token_stats, rollout_details=rollout_data)


class CapacityEstimate(BaseModel):
    by_average: int
    by_median: int
    conservative_max: int


class CapacityResult(BaseModel):
    context_limit: int
    overhead_tokens: int
    usable_context: int
    avg_rollout_size: float
    max_rollout_size: float
    median_rollout_size: float
    estimated_capacity: CapacityEstimate
    rollout_stats: AnalysisStats


def analyze_rollout_capacity(log_path: Path, context_limit: int = 200000) -> CapacityResult | None:
    """Analyze rollout logs and estimate context capacity."""
    stats = analyze_rollout_logs(log_path)
    if not stats:
        return None

    avg_rollout_size = stats.token_stats["total_tokens"].avg
    max_rollout_size = stats.token_stats["total_tokens"].max
    median_rollout_size = stats.token_stats["total_tokens"].median

    # Estimate capacity (leaving room for system message and prompt engineering overhead)
    overhead_tokens = 5000  # Conservative estimate for system message + PE overhead
    usable_context = context_limit - overhead_tokens

    return CapacityResult(
        context_limit=context_limit,
        overhead_tokens=overhead_tokens,
        usable_context=int(usable_context),
        avg_rollout_size=float(avg_rollout_size),
        max_rollout_size=float(max_rollout_size),
        median_rollout_size=float(median_rollout_size),
        estimated_capacity=CapacityEstimate(
            by_average=int(usable_context // avg_rollout_size),
            by_median=int(usable_context // median_rollout_size),
            conservative_max=int(usable_context // max_rollout_size),
        ),
        rollout_stats=stats,
    )
