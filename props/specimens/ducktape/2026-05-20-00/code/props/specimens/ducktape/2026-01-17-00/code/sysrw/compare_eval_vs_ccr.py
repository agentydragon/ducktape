#!/usr/bin/env python3
import argparse
import difflib
import json
from pathlib import Path
from typing import Any

TRACE_DIR = Path.home() / ".claude-code-router" / "logs"


def load_samples(run_dir: Path) -> list[dict[str, Any]]:
    samp_path = run_dir / "samples.jsonl"
    out: list[dict[str, Any]] = []
    with samp_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def find_ccr_openai_request(correlation_id: str) -> dict[str, Any] | None:
    # Scan logs for outbound_request to OpenAI chat completions with this correlationId
    files = sorted(TRACE_DIR.glob("trace.*"))
    for p in files:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") != "outbound_request":
                    continue
                if rec.get("correlationId") != correlation_id:
                    continue
                url = rec.get("url", "")
                if not isinstance(url, str) or "/v1/chat/completions" not in url:
                    continue
                body = rec.get("body")
                if isinstance(body, dict):
                    return body
    return None


def find_ccr_requests_for(correlation_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Scan CCR logs once and collect outbound chat completion requests for the given IDs.
    Early-exits when all targets are found.
    """
    if not correlation_ids:
        return {}
    remaining = set(correlation_ids)
    found: dict[str, dict[str, Any]] = {}
    files = sorted(TRACE_DIR.glob("trace.*"))
    for p in files:
        if not remaining:
            break
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not remaining:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") != "outbound_request":
                    continue
                cid = rec.get("correlationId")
                if cid not in remaining:
                    continue
                url = rec.get("url", "")
                if not isinstance(url, str) or "/v1/chat/completions" not in url:
                    continue
                body = rec.get("body")
                if isinstance(body, dict):
                    found[cid] = body
                    remaining.discard(cid)
    return found


def drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)


def unified_diff_str(a: str, b: str, fromfile: str, tofile: str) -> str:
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    diff = difflib.unified_diff(a_lines, b_lines, fromfile=fromfile, tofile=tofile)
    return "".join(diff)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, help="Path to eval run directory (contains samples.jsonl)")
    ap.add_argument(
        "--out-dir", required=False, help="Output directory for diffs; defaults to <run-dir>/compare_vs_ccr"
    )
    ap.add_argument("--limit", type=int, default=5, help="Max number of samples to compare")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    out_dir = Path(args.out_dir) if args.out_dir else (run_dir / "compare_vs_ccr")
    out_dir.mkdir(parents=True, exist_ok=True)

    samples = load_samples(run_dir)
    # Preselect up to limit targets that have a correlation_id and request
    targets: list[dict[str, Any]] = []
    target_ids: set[str] = set()
    for rec in samples:
        if len(targets) >= args.limit:
            break
        cid = rec.get("correlation_id")
        eval_req = rec.get("request") or {}
        if not isinstance(cid, str) or not isinstance(eval_req, dict):
            continue
        targets.append(rec)
        target_ids.add(cid)

    # Single pass over CCR logs to collect needed requests
    ccr_map = find_ccr_requests_for(target_ids)

    count = 0
    wrote: list[str] = []
    for rec in targets:
        cid = rec.get("correlation_id")
        eval_req = rec.get("request") or {}
        if not isinstance(cid, str):
            continue
        ccr_req = ccr_map.get(cid)
        if not ccr_req:
            continue
        # Prepare pretty JSONs
        eval_body = drop_none(dict(eval_req))
        eval_json = pretty(eval_body)
        ccr_json = pretty(ccr_req)
        # Write files
        case_dir = out_dir / f"cid-{cid}"
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "eval_request.json").write_text(eval_json, encoding="utf-8")
        (case_dir / "ccr_request.json").write_text(ccr_json, encoding="utf-8")
        # Diff
        diff_text = unified_diff_str(ccr_json, eval_json, fromfile="ccr_request.json", tofile="eval_request.json")
        (case_dir / "diff.unified.txt").write_text(diff_text, encoding="utf-8")
        wrote.append(str(case_dir))
        count += 1

    summary_path = out_dir / "SUMMARY.txt"
    summary_path.write_text("\n".join(wrote), encoding="utf-8")
    print(json.dumps({"compared": count, "out_dir": str(out_dir)}))


if __name__ == "__main__":
    main()
