#!/usr/bin/env python3
"""Publish one reviewed, source-pinned snapshot in its history namespace."""

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Full SHA of inspected devel commit")
    parser.add_argument("--window-start", required=True, help="UTC ISO-8601")
    parser.add_argument("--window-end", required=True, help="UTC ISO-8601")
    parser.add_argument("--kind", required=True, choices=["latency", "reliability"], help="Report namespace")
    parser.add_argument(
        "--report", required=True, type=Path, help="Authored standalone HTML report copied to index.html"
    )
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--attribution", type=Path)
    parser.add_argument("--out", required=True, type=Path, help="Root of the ci-reports worktree")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source):
        parser.error("source must be a full SHA")

    def parse(s):
        d = datetime.fromisoformat(s)
        offset = d.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            parser.error("window must be UTC")
        return d

    if parse(args.window_start) >= parse(args.window_end):
        parser.error("window end must follow start")
    if git("cat-file", "-t", args.source) != "commit":
        parser.error("source is not an available commit")
    evidence = json.loads(args.evidence.read_text())
    attribution = json.loads(args.attribution.read_text()) if args.attribution else None
    report = args.report.read_bytes()
    snapshot = args.out / args.kind
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "report.md").unlink(missing_ok=True)
    (snapshot / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    if attribution is not None:
        (snapshot / "attribution.json").write_text(json.dumps(attribution, indent=2) + "\n")
    else:
        (snapshot / "attribution.json").unlink(missing_ok=True)
    manifest = {
        "source_devel_commit": args.source,
        "window_start_utc": args.window_start,
        "window_end_utc": args.window_end,
        "published_at_utc": datetime.now(UTC).isoformat(),
        "attribution": "measured" if attribution else "not collected",
    }
    (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (snapshot / "index.html").write_bytes(report)


if __name__ == "__main__":
    main()
