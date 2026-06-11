#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from openai_utils.client_factory import get_async_openai
from sysrw import compare_eval_vs_ccr, extract_dataset_ccr, extract_dataset_crush, leaderboard, run_eval
from sysrw.templates.loader import validate_template_file

app = typer.Typer(help="System rewriter toolkit: extract datasets, run evals, and compare against CCR.")

# Typer singletons to avoid B008 in function defaults
ARG_TEMPLATE = typer.Argument(..., help="Path to system prompt template (mustache-style: {{toolsBlob}}, etc.)")
ARG_RUN_DIR = typer.Argument(..., help="Path to eval run directory (contains samples.jsonl)")

OPT_DATASET = typer.Option(
    None,
    "--dataset",
    "-d",
    help=("Dataset JSONL path(s); repeat to mix CCR and Crush samples. Defaults to built-in dataset if omitted."),
)
OPT_OUT_DIR_RUN = typer.Option(None, "--out-dir", help="Output directory. If omitted, writes to runs/<ts>.")
OPT_N = typer.Option(None, "--n", help="Limit number of samples to process")
OPT_CONCURRENCY = typer.Option(32, "--concurrency", help="Parallelism for sampling/grading")

OPT_SOURCE = typer.Option("auto", "--source", help="ccr|crush|auto")
OPT_WIRE_LOG = typer.Option(None, "--wire-log", help="Crush only: path to provider-wire.log")
OPT_SCAN_DIR = typer.Option(
    None, "--scan-dir", help="Crush only: scan DIR recursively for **/.crush/logs/provider-wire.log (repeatable)"
)
OPT_OUTPUT = typer.Option(None, "--output", help="Output JSONL path (default depends on source)")
OPT_RUNS_DIR = typer.Option(
    Path(__file__).parent / "runs", "--runs-dir", help="Directory containing eval runs (runs/<ts>)"
)
OPT_COMPARE_OUT_DIR = typer.Option(None, "--out-dir", help="Output directory for diffs")
OPT_COMPARE_LIMIT = typer.Option(5, "--limit", help="Max number of samples to compare")


@app.command("run")
def cmd_run(
    template: Path = ARG_TEMPLATE,
    dataset: list[Path] = OPT_DATASET,
    out_dir: Path | None = OPT_OUT_DIR_RUN,
    n: int | None = OPT_N,
    concurrency: int = OPT_CONCURRENCY,
):
    """Run an evaluation end-to-end (rewrite → sample → grade → report)."""
    dsets = dataset or [run_eval.DEFAULT_DATASET_PATH]
    base_out = out_dir if out_dir else None
    # Fail fast on invalid/unreadable template
    validate_template_file(template)
    asyncio.run(
        run_eval.run_eval(
            template_path=template,
            dataset_paths=dsets,
            base_out=base_out,
            n_limit=n,
            concurrency=concurrency,
            client=get_async_openai(),
        )
    )


@app.command("compare")
def cmd_compare(
    run_dir: Path = ARG_RUN_DIR, out_dir: Path | None = OPT_COMPARE_OUT_DIR, limit: int = OPT_COMPARE_LIMIT
):
    """Diff eval sampler requests vs actual CCR chat completion requests."""
    out = out_dir or (run_dir / "compare_vs_ccr")
    out.mkdir(parents=True, exist_ok=True)

    samples = compare_eval_vs_ccr.load_samples(run_dir)
    count = 0
    wrote: list[str] = []
    for rec in samples:
        if count >= limit:
            break
        cid = rec.get("correlation_id")
        eval_req = rec.get("request") or {}
        if not cid or not isinstance(eval_req, dict):
            continue
        ccr_req = compare_eval_vs_ccr.find_ccr_openai_request(cid)
        if not ccr_req:
            continue
        # Prepare pretty JSONs
        eval_body = compare_eval_vs_ccr.drop_none(dict(eval_req))
        eval_json = compare_eval_vs_ccr.pretty(eval_body)
        ccr_json = compare_eval_vs_ccr.pretty(ccr_req)
        # Write files
        case_dir = out / f"cid-{cid}"
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "eval_request.json").write_text(eval_json, encoding="utf-8")
        (case_dir / "ccr_request.json").write_text(ccr_json, encoding="utf-8")
        # Diff
        diff_text = compare_eval_vs_ccr.unified_diff_str(
            ccr_json, eval_json, fromfile="ccr_request.json", tofile="eval_request.json"
        )
        (case_dir / "diff.unified.txt").write_text(diff_text, encoding="utf-8")
        wrote.append(str(case_dir))
        count += 1

    summary_path = out / "SUMMARY.txt"
    summary_path.write_text("\n".join(wrote), encoding="utf-8")
    print(json.dumps({"compared": count, "out_dir": str(out)}))


@app.command("extract")
def cmd_extract(
    source: str = OPT_SOURCE,
    wire_log: Path | None = OPT_WIRE_LOG,
    scan_dir: list[Path] = OPT_SCAN_DIR,
    output: Path | None = OPT_OUTPUT,
):
    """Unified dataset extractor for CCR and Crush logs."""
    src = source.lower()
    if src == "auto":
        src = "crush" if (wire_log or (scan_dir and len(scan_dir) > 0)) else "ccr"

    if src == "crush":
        out_path = output or extract_dataset_crush.OUTPUT_PATH
        out_path.parent.mkdir(parents=True, exist_ok=True)
        logs: list[Path] = []
        if wire_log:
            logs = [wire_log]
        else:
            # Prefer default single log if it exists, else scan ~/.crush, else scan provided dirs
            default_log = extract_dataset_crush.DEFAULT_WIRE_LOG
            roots = scan_dir or [Path.home() / ".crush"]
            logs = []
            if isinstance(default_log, Path) and default_log.exists():
                logs.append(default_log)
            logs.extend(extract_dataset_crush.find_wire_logs(roots))
            # Dedup while preserving order (avoid set.add() in expression for mypy correctness)
            seen: set[str] = set()
            deduped: list[Path] = []
            for p in logs:
                sp = str(p)
                if sp in seen:
                    continue
                seen.add(sp)
                deduped.append(p)
            logs = deduped
        total = 0
        with out_path.open("w", encoding="utf-8") as out_f:
            for log_path in logs:
                recs = extract_dataset_crush.process_wire(log_path, require_bad=True)
                for r in recs:
                    out_f.write(json.dumps(r, ensure_ascii=False) + "\n")
                total += len(recs)
        print(
            json.dumps(
                {"event": "dataset_crush_written", "count": total, "path": str(out_path), "files_scanned": len(logs)}
            )
        )
        return

    if src == "ccr":
        asyncio.run(extract_dataset_ccr.main())
        return

    raise typer.BadParameter("--source must be one of: ccr, crush, auto")


@app.command("leaderboard")
def cmd_leaderboard(
    runs_dir: Path = OPT_RUNS_DIR,
    sort_key: str = typer.Option("mean", "--sort", help="Sort key: mean|lcb|ucb"),
    asc: bool = typer.Option(False, "--asc", help="Sort ascending"),
    limit: int | None = typer.Option(None, "--limit", help="Limit rows"),
    # --since removed; grouping by template consolidates runs regardless of timestamp
) -> None:
    table, errors, missing = leaderboard.generate(runs_dir=runs_dir, sort_key=sort_key, asc=asc, limit=limit)
    console = Console()
    console.print(table)
    if missing:
        console.print(Rule("Templates not yet evaluated", style="yellow"))
        for name in sorted(missing):
            console.print(f"- {name}")
    if errors:
        console.print(Rule("Template validation errors", style="red"))
        for e in errors:
            console.print(Panel(e, border_style="red"))


if __name__ == "__main__":
    app()
