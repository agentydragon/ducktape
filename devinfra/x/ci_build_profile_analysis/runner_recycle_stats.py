#!/usr/bin/env python3
"""Estimate how often bbr/CI Firecracker runners reuse a snapshot vs cold-start.

Each `bbr`/CI run dispatches a ci_runner Firecracker VM (the `HOSTED_BAZEL`
"remote ..." invocation, configured in devinfra/bbr.json with recycle-runner,
remote-snapshot-save-policy=always, snapshot-read-policy=newest). The runner's
first console lines reveal whether the VM resumed from a snapshot:

  warm (snapshot reused):  "Syncing existing repo..." -> shallow git fetch +
                           git clean + git checkout. repo and Bazel output-base
                           survive, so external repos and Bazel server state may
                           be available.
  cold (fresh VM):         "Cloning ..." -> full clone, re-fetch every external
                           repo, reload all packages ("redoing repo fetch work").

This classifies only the outer runner VM. Use BES metrics and Bazel profiles to
answer whether the inner Bazel analysis cache did any work. There is no clean
structured field for runner warm/cold state; the console header is authoritative.
We list runner invocations via `bbapi`, fetch each runner's console via `bb view`,
and classify.

Usage:
  ./runner_recycle_stats.py [--count N] [--workers W] [--gaps-only] [--json]

  --count       invocations to pull from bbapi (default 600; the API pages
                internally by recency, so larger N == wider time window)
  --gaps-only   only classify the first runner invocation after each idle gap
                >10min (the cold-start candidates) instead of every runner
  --workers     parallel `bb view` fetches (default 12)

Requires BUILDBUDDY_API_KEY (session hook / Nix devshell sets it) and `bb` +
`bbapi` on PATH.
"""

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys

# warm marker first; order matters (warm runners also touch the repo).
_MARKERS = re.compile(
    r"Syncing existing repo|Cloning into|Cloning repository|"
    r"Initializing|Cleaning workspace|Configuring repository",
    re.IGNORECASE,
)


def list_runner_invocations(count: int) -> list[dict]:
    """Runner (ci_runner VM) invocations, oldest-first. These are the
    HOSTED_BAZEL `remote ...` invocations, not the inner bazel children."""
    out = subprocess.run(
        ["bbapi", "invocation", "list", "--count", str(count), "--json"], capture_output=True, text=True, check=True
    ).stdout
    invs = json.loads(out)["invocation"]
    runners = [i for i in invs if i["command"].startswith("remote ")]
    return sorted(runners, key=lambda i: int(i["createdAtUsec"]))


def post_gap_candidates(runners: list[dict], gap_min: float) -> set[str]:
    """IDs of the first runner after each idle gap — the cold-start candidates."""
    cands: set[str] = set()
    prev = None
    for i in runners:
        t = int(i["createdAtUsec"]) / 1e6
        if prev is not None and (t - prev) > gap_min * 60:
            cands.add(i["invocationId"])
        prev = t
    return cands


def classify(invocation_id: str) -> str:
    """warm | cold | unknown, from the runner's console header."""
    proc = subprocess.run(["bb", "view", invocation_id], check=False, capture_output=True, text=True)
    header = "\n".join(proc.stdout.splitlines()[:10])
    m = _MARKERS.search(header)
    if not m:
        return "unknown"
    return "warm" if m.group(0).lower().startswith("syncing") else "cold"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=600)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--gaps-only", action="store_true")
    ap.add_argument("--gap-min", type=float, default=10.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    runners = list_runner_invocations(args.count)
    ts = [int(i["createdAtUsec"]) / 1e6 for i in runners]
    span_h = (max(ts) - min(ts)) / 3600 if len(ts) > 1 else 0.0

    ids = post_gap_candidates(runners, args.gap_min) if args.gaps_only else {i["invocationId"] for i in runners}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        verdicts = dict(zip(ids, ex.map(classify, ids), strict=True))

    counts = {"warm": 0, "cold": 0, "unknown": 0}
    for v in verdicts.values():
        counts[v] += 1
    total = len(verdicts)
    warm_pct = 100 * counts["warm"] / total if total else 0.0

    if args.json:
        json.dump(
            {
                "runner_invocations_in_window": len(runners),
                "window_hours": round(span_h, 1),
                "classified": total,
                "gaps_only": args.gaps_only,
                "counts": counts,
                "warm_pct": round(warm_pct, 1),
                "verdicts": verdicts,
            },
            sys.stdout,
            indent=2,
        )
        print()
        return

    print(
        f"window: {span_h:.1f}h  runner invocations: {len(runners)}  "
        f"classified: {total}{' (post-gap firsts only)' if args.gaps_only else ''}"
    )
    print(f"  warm (snapshot reused): {counts['warm']}")
    print(f"  cold (fresh VM):        {counts['cold']}")
    if counts["unknown"]:
        print(f"  unknown:                {counts['unknown']}")
    print(f"  warm rate: {warm_pct:.1f}%")


if __name__ == "__main__":
    main()
