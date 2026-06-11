"""Output helpers for Twenty Questions eval runs."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from skills.info_gathering.evals.twenty_questions.result_types import RunSummary

logger = logging.getLogger(__name__)


def run_output_paths(name: str, output_dir: Path) -> tuple[Path, Path]:
    """Create output_dir and return (calls_jsonl_path, summary_json_path)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    prefix = output_dir / f"{name}_{ts}"
    calls_path = prefix.with_name(prefix.name + "_calls.jsonl")
    summary_path = prefix.with_name(prefix.name + "_summary.json")
    return calls_path, summary_path


def save_summary(*, summary: RunSummary, summary_path: Path) -> None:
    summary_path.write_text(summary.model_dump_json(indent=2))
    logger.info("Saved results to %s", summary_path.parent)
