"""Leaderboard reporter for eval runs (packaged).

Loads templates from templates/ (baseline and proposals), matches run
template hashes to these names, and prints a sorted leaderboard.
Marks any run whose template isn't in templates/.

Defaults to rich table output sorted by mean score desc.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.table import Table

from .templates import load_known_templates, validate_template_file


@dataclass
class Row:
    run: str
    mean: float
    ci95: float
    n: int
    lcb: float | None
    ucb: float | None
    template_label: str
    template_hash: str
    with_tools_pct: float
    sampler_model: str | None = None
    grader_model: str | None = None
    template_error: bool = False
    template_error_exc: str | None = None


class SummaryCI(BaseModel):
    lcb: float | None = None
    ucb: float | None = None
    model_config = ConfigDict(extra="ignore")


class SummaryModels(BaseModel):
    sampler: str | None = None
    evaluator: str | None = None
    grader: str | None = None
    model_config = ConfigDict(extra="ignore")


class SummaryTooling(BaseModel):
    with_tools_pct: float | None = 0.0
    text_only_pct: float | None = None
    total_samples: int | None = None
    model_config = ConfigDict(extra="ignore")


class EvalSummary(BaseModel):
    n: int = 0
    mean: float = 0.0
    ci95: SummaryCI | float | None = None
    lcb: float | None = None
    ucb: float | None = None
    tooling: SummaryTooling | None = None
    models: SummaryModels | None = None
    model_config = ConfigDict(extra="ignore")


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def iter_run_dirs(runs_dir: Path) -> Iterable[Path]:
    yield from sorted([d for d in runs_dir.iterdir() if d.is_dir()])


def find_summary_and_template(run_dir: Path) -> tuple[Path | None, Path | None]:
    s = run_dir / "summary.json"
    t = run_dir / "template.txt"
    if s.exists() and t.exists():
        return s, t
    # Legacy single nested dir fallback
    try:
        subs = [d for d in run_dir.iterdir() if d.is_dir()]
    except FileNotFoundError:
        return None, None
    if len(subs) == 1 and (subs[0] / "summary.json").exists():
        return subs[0] / "summary.json", subs[0] / "template.txt"
    return None, None


def load_row(run_dir: Path, known: dict[str, str]) -> Row | None:
    s_path, t_path = find_summary_and_template(run_dir)
    if not (s_path and t_path):
        return None
    raw = json.loads(s_path.read_text(encoding="utf-8"))
    summ = EvalSummary.model_validate(raw)
    t_text = t_path.read_text(encoding="utf-8")
    thash = sha1_text(t_text) if t_text else "?"
    label = known.get(thash) or str(t_path)

    # Validate the concrete template file in this run
    is_err = False
    err_repr: str | None = None
    try:
        validate_template_file(t_path)
    except (OSError, ValueError, TypeError) as exc:
        is_err = True
        err_repr = repr(exc)

    mean = float(summ.mean)

    # Derive ci95/bounds via typed helper
    ci95_val, lcb, ucb = _derive_ci_from_summary(summ)

    tooling = summ.tooling
    with_tools = float(tooling.with_tools_pct) if tooling and tooling.with_tools_pct is not None else 0.0

    # Extract models (sampler/evaluator) for display; do not persist/modify files here
    sampler_model: str | None = None
    grader_model: str | None = None
    models = summ.models
    if models is not None:
        sampler_model = models.sampler
        grader_model = models.evaluator or models.grader

    # TODO(mpokorny): deprecate legacy numeric ci95 once all runs use ci95={lcb,ucb}.

    return Row(
        run=run_dir.name,
        mean=mean,
        ci95=float(ci95_val),
        n=int(summ.n or 0),
        lcb=lcb,
        ucb=ucb,
        template_label=label,
        template_hash=thash,
        with_tools_pct=with_tools,
        sampler_model=sampler_model,
        grader_model=grader_model,
        template_error=is_err,
        template_error_exc=err_repr,
    )


def _color_for_hash(hash_hex: str) -> str:
    # Kept for palette choice; may change to deterministic by template text if desired
    # Deterministic palette index from hash
    palette = [
        "bright_cyan",
        "bright_magenta",
        "bright_green",
        "bright_yellow",
        "bright_blue",
        "bright_red",
        "cyan",
        "magenta",
        "green",
        "yellow",
        "blue",
        "red",
    ]
    try:
        idx = int(hash_hex[:8], 16) % len(palette)
    except ValueError:
        idx = 0
    return palette[idx]


def _relpath(p: str) -> str:
    # Prefer a path relative to current working directory when possible
    try:
        abs_p = str(Path(p).resolve())
    except OSError:
        abs_p = p
    try:
        return os.path.relpath(abs_p, start=Path.cwd())
    except (OSError, ValueError):
        return p


def format_rich_table(rows: list[Row]) -> Table:
    """Build a rich.Table; caller is responsible for printing with Console."""
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("lcb", justify="right")
    table.add_column("mean", justify="right")
    table.add_column("ucb", justify="right")
    table.add_column("n", justify="right")
    table.add_column("models", justify="left")
    table.add_column("tools%", justify="right")
    table.add_column("run", justify="left")
    table.add_column("template", justify="left")

    for r in rows:
        # Derive display bounds via helper when missing
        lcb, ucb = _bounds_or_fallback(r.mean, r.ci95, r.lcb, r.ucb)
        color = _color_for_hash(r.template_hash)
        if r.template_error:
            label_cell = f"[bold red]ERR: {_relpath(r.template_label)}[/]"
        else:
            rel_label = _relpath(r.template_label)
            label_cell = f"[{color}]{rel_label}[/]"
        table.add_row(
            f"{lcb:.2f}",
            f"{r.mean:.2f}",
            f"{ucb:.2f}",
            f"{r.n}",
            f"{(r.sampler_model or 'gpt-5')}|{(r.grader_model or 'gpt-5')}",
            f"{r.with_tools_pct * 100:.1f}%",
            r.run,
            label_cell,
        )

    return table


def generate(
    runs_dir: Path, sort_key: str = "mean", asc: bool = False, limit: int | None = None
) -> tuple[Table, list[str], list[str]]:
    # Build mapping from packaged templates/ only (stable names)
    known = load_known_templates()
    if not runs_dir.exists():
        raise FileNotFoundError(f"No runs dir: {runs_dir}")

    # Build groups by template hash
    by_hash: dict[str, list[Row]] = {}
    for rd in iter_run_dirs(runs_dir):
        row = load_row(rd, known)
        if not row:
            continue
        by_hash.setdefault(row.template_hash, []).append(row)

    # Determine which packaged templates have no eval runs
    known_hash_to_name: dict[str, str] = {sha1_text(text): name for text, name in known.items()}
    missing_templates: list[str] = [name for h, name in known_hash_to_name.items() if h not in by_hash]

    # Row sort key
    def _row_key(r: Row) -> float:
        if sort_key == "mean":
            return r.mean
        lcb_val, ucb_val = _bounds_or_fallback(r.mean, r.ci95, r.lcb, r.ucb)
        return lcb_val if sort_key == "lcb" else ucb_val

    row_key = _row_key

    # Sort within groups and then groups by best row
    groups: list[list[Row]] = []
    for rows in by_hash.values():
        rows.sort(key=row_key, reverse=not asc)
        groups.append(rows)
    groups.sort(key=lambda g: row_key(g[0]) if g else float("-inf"), reverse=not asc)

    # Flatten
    flat_rows: list[Row] = [r for g in groups for r in g]
    if limit is not None:
        flat_rows = flat_rows[: max(0, limit)]

    table = format_rich_table(flat_rows)

    # Collect error reprs for any invalid templates
    errors: list[str] = []
    for r in flat_rows:
        if r.template_error:
            errors.append(f"{r.run}: {_relpath(r.template_label)} — {r.template_error_exc}")

    return table, errors, missing_templates


def parse_args() -> argparse.Namespace:
    # Note: retained for direct CLI use via adgn-sysrw leaderboard; not used as module API.
    ap = argparse.ArgumentParser(description="Report leaderboard for eval runs (runs/<ts> → summary).")
    ap.add_argument(
        "--runs-dir",
        type=Path,
        default=Path(__file__).parent / "runs",
        help="Directory containing run folders (default: ./runs)",
    )
    ap.add_argument("--sort", choices=["mean", "lcb", "ucb"], default="mean", help="Sort key (default: mean)")
    ap.add_argument("--asc", action="store_true", default=False, help="Sort ascending")
    ap.add_argument("--limit", type=int, default=None)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    try:
        table, errors, missing = generate(
            runs_dir=args.runs_dir, sort_key=args.sort, asc=bool(args.asc), limit=args.limit
        )
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 2
    console = Console()
    console.print(table)
    if missing:
        console.print("Templates not yet evaluated:\n" + "\n".join(f"- {name}" for name in sorted(missing)))
    for err in errors:
        console.print(f"[red]{err}[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def _derive_ci_from_summary(summary: EvalSummary) -> tuple[float, float | None, float | None]:
    """Return (ci95_halfwidth, lcb, ucb) from a typed summary.

    Accepts legacy numeric ci95 or a dict with lcb/ucb. If only bounds are
    available, derives the half-width as max(|mean-lcb|, |ucb-mean|). Falls back
    to 0.0 when nothing usable is present.
    """
    mean = float(summary.mean)
    ci95_half: float | None = None
    lcb: float | None = None
    ucb: float | None = None

    if isinstance(summary.ci95, SummaryCI):
        lcb = summary.ci95.lcb
        ucb = summary.ci95.ucb
        if lcb is not None and ucb is not None:
            ci95_half = max(abs(mean - lcb), abs(ucb - mean))
    elif isinstance(summary.ci95, int | float):
        ci95_half = float(summary.ci95)
    # Fallback: top-level bounds if provided
    if lcb is None and summary.lcb is not None:
        lcb = float(summary.lcb)
    if ucb is None and summary.ucb is not None:
        ucb = float(summary.ucb)

    if ci95_half is None:
        ci95_half = max(abs(mean - lcb), abs(ucb - mean)) if lcb is not None and ucb is not None else 0.0
    return ci95_half, lcb, ucb


def _bounds_or_fallback(mean: float, ci95: float, lcb: float | None, ucb: float | None) -> tuple[float, float]:
    """Return concrete (lcb, ucb), using mean±ci95 if bounds are missing."""
    lcb_val = lcb if lcb is not None else (mean - ci95)
    ucb_val = ucb if ucb is not None else (mean + ci95)
    return (lcb_val, ucb_val)
