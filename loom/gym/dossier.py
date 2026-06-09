"""Per-task input data: the series truncated to the task's `as_of`.

The dossier is part of the **task definition**, not the contestant: every
contestant receives exactly these bytes — inlined into a prompt (bare LLM
baseline with `--with-data`) or materialized as files in the network-less
container harness — so contestants stay apples-to-apples.

Truncation: a month's value is knowable once the month has ended, so the
dossier contains months strictly before `as_of` (for series tasks anchored at
month M, `as_of` is the first day of M+1, which includes the anchor month).

Known leakage nuance: the published series are today's *revised* values; a
true as-of dossier would use first-print vintages (ALFRED for the FRED
series). Tracked as a G1-grade improvement.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

from loom.gym.monthly_series import MonthlySeries


def series_dossier(series: Sequence[MonthlySeries], as_of: date) -> dict[str, str]:
    """Filename → file content for the given series, truncated to months < `as_of`."""
    files = {}
    readme_lines = [f"Data files, each `month,value` CSV rows through {as_of} (exclusive):"]
    for one_series in series:
        rows = sorted((month, value) for month, value in one_series.values.items() if month < as_of)
        files[f"{one_series.series_id}_monthly.csv"] = "month,value\n" + "".join(
            f"{month:%Y-%m},{value}\n" for month, value in rows
        )
        readme_lines.append(f"- {one_series.series_id}_monthly.csv: {one_series.description}, in {one_series.unit}.")
    files["README.txt"] = "\n".join(readme_lines) + "\n"
    return files


def materialize_dossier(dossier: dict[str, str], directory: Path) -> None:
    """Write dossier files into `directory` — the container harness's mount source."""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in dossier.items():
        (directory / filename).write_text(content)
