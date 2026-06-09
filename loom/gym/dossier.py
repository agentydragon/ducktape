"""Per-task input data: the frozen series CSVs truncated to the task's `as_of`.

The dossier is part of the **task definition**, not the contestant: every
contestant receives exactly these bytes — inlined into a prompt (bare LLM
baseline with `--with-data`) or materialized as files in the network-less
container harness — so contestants stay apples-to-apples.

Truncation: a month's value is knowable once the month has ended, so the
dossier contains months strictly before `as_of` (for series tasks anchored at
month M, `as_of` is the first day of M+1, which includes the anchor month).

Known leakage nuance: the frozen CSVs are today's *revised* series; a true
as-of dossier would use first-print vintages (ALFRED for the FRED series).
Tracked as a G1-grade improvement.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from loom.gym.monthly_series import default_series


def series_dossier(as_of: date) -> dict[str, str]:
    """Filename → file content for all default series, truncated to months < `as_of`."""
    files = {}
    readme_lines = [f"Data files, each `month,value` CSV rows through {as_of} (exclusive):"]
    for series in default_series():
        rows = sorted((month, value) for month, value in series.values.items() if month < as_of)
        files[f"{series.series_id}_monthly.csv"] = "month,value\n" + "".join(
            f"{month:%Y-%m},{value}\n" for month, value in rows
        )
        readme_lines.append(f"- {series.series_id}_monthly.csv: {series.description}, in {series.unit}.")
    files["README.txt"] = "\n".join(readme_lines) + "\n"
    return files


def materialize_dossier(dossier: dict[str, str], directory: Path) -> None:
    """Write dossier files into `directory` — the container harness's mount source."""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in dossier.items():
        (directory / filename).write_text(content)
