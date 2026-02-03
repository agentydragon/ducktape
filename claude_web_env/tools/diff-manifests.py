#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic", "pyyaml"]
# ///
"""Compare two NDJSON filesystem manifests with narrow exclusion support.

Default: bit-for-bit comparison. Exclusions are loaded from a YAML/JSON config
file and apply *very narrow* rules:

  - "skip_paths": ["/var/log", "/dev"]
      Paths under these prefixes are ignored entirely.

  - "hash_may_differ": ["/opt/rbenv/versions/**", "/root/.cargo/**"]
      Both sides must have the file, but sha256 may differ (compiled artifacts).

  - "only_in_live": ["/process_api", "/usr/local/bin/environment-manager"]
      These paths are expected to only exist in the live container
      (proprietary binaries not reproducible from public sources).

Usage:
    ./diff-manifests.py live.ndjson built.ndjson
    ./diff-manifests.py live.ndjson built.ndjson --exclusions exclusions.yaml
    ./diff-manifests.py live.ndjson built.ndjson -o report.md
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from manifest import Entry, Exclusions, load_exclusions, parse_ndjson
from pydantic import BaseModel

REAL_DIFF_STATUSES = {
    "only_left",
    "only_right",
    "type_changed",
    "content_changed",
    "link_changed",
    "metadata_changed",
}


class DiffResult(BaseModel):
    path: str
    status: str  # match, excluded, hash_excluded, only_left, only_right,
    # expected_only_left, expected_only_right,
    # type_changed, content_changed, link_changed, metadata_changed
    details: str = ""
    left: Entry | None = None
    right: Entry | None = None


def diff_manifests(
    left: dict[str, Entry],
    right: dict[str, Entry],
    excl: Exclusions,
) -> list[DiffResult]:
    all_paths = sorted(set(left) | set(right))
    results: list[DiffResult] = []

    for path in all_paths:
        if excl.should_skip(path):
            results.append(DiffResult(path=path, status="excluded"))
            continue

        le = left.get(path)
        re = right.get(path)

        if le and not re:
            if excl.expected_only_in_live(path) or excl.is_volatile(path):
                results.append(DiffResult(path=path, status="expected_only_left", left=le))
            else:
                results.append(DiffResult(path=path, status="only_left", left=le))
            continue
        if re and not le:
            if excl.expected_only_in_built(path) or excl.is_volatile(path):
                results.append(DiffResult(path=path, status="expected_only_right", right=re))
            else:
                results.append(DiffResult(path=path, status="only_right", right=re))
            continue

        assert le and re

        # Volatile paths: any difference is expected
        volatile = excl.is_volatile(path)

        if le.type != re.type:
            if volatile:
                results.append(DiffResult(path=path, status="hash_excluded", details="volatile", left=le, right=re))
                continue
            results.append(
                DiffResult(
                    path=path, status="type_changed", details=f"{le.type}->{re.type}", left=le, right=re
                )
            )
            continue

        if le.type == "l" and le.link_target != re.link_target:
            if volatile:
                results.append(DiffResult(path=path, status="hash_excluded", details="volatile", left=le, right=re))
                continue
            results.append(
                DiffResult(
                    path=path,
                    status="link_changed",
                    details=f"{le.link_target}->{re.link_target}",
                    left=le,
                    right=re,
                )
            )
            continue

        if le.type == "f" and le.sha256 and re.sha256 and le.sha256 != re.sha256:
            if excl.hash_ok_to_differ(path) or volatile:
                results.append(
                    DiffResult(
                        path=path,
                        status="hash_excluded",
                        details="hash differs (expected)",
                        left=le,
                        right=re,
                    )
                )
                continue
            results.append(
                DiffResult(
                    path=path,
                    status="content_changed",
                    details=f"size {le.size}->{re.size}",
                    left=le,
                    right=re,
                )
            )
            continue

        changes = []
        if not excl.ignore_perms and le.perms != re.perms:
            changes.append(f"perms {le.perms}->{re.perms}")
        if not excl.ignore_owner and le.owner != re.owner:
            changes.append(f"owner {le.owner}->{re.owner}")
        if not excl.ignore_group and le.group != re.group:
            changes.append(f"group {le.group}->{re.group}")
        if changes:
            if volatile:
                results.append(DiffResult(path=path, status="hash_excluded", details="volatile", left=le, right=re))
                continue
            results.append(
                DiffResult(
                    path=path, status="metadata_changed", details=", ".join(changes), left=le, right=re
                )
            )
            continue

        results.append(DiffResult(path=path, status="match", left=le, right=re))

    return results


def categorize_path(path: str) -> str:
    prefixes = [
        ("/usr/lib/python", "python-libs"),
        ("/usr/lib/jvm", "java"),
        ("/usr/lib/x86_64", "system-libs"),
        ("/usr/share/doc", "docs"),
        ("/usr/share/man", "man-pages"),
        ("/usr/share", "usr-share"),
        ("/usr/include", "headers"),
        ("/usr/local", "usr-local"),
        ("/usr/bin", "system-binaries"),
        ("/usr/sbin", "system-binaries"),
        ("/opt/node", "nodejs"),
        ("/opt/ruby", "ruby"),
        ("/opt/rbenv", "ruby"),
        ("/opt/gradle", "java-build"),
        ("/opt/apache-maven", "java-build"),
        ("/opt/nvm", "nvm"),
        ("/opt", "opt-other"),
        ("/root/.rustup", "rust"),
        ("/root/.cargo", "rust"),
        ("/root/.bun", "bun"),
        ("/root/.local/share/uv", "uv-tools"),
        ("/root/.local", "root-local"),
        ("/root/.claude", "claude-config"),
        ("/root", "root-home"),
        ("/home", "home"),
        ("/etc", "etc"),
        ("/var", "var"),
    ]
    for prefix, cat in prefixes:
        if path.startswith(prefix):
            return cat
    return "other"


def generate_report(results: list[DiffResult], left_label: str, right_label: str) -> str:
    lines: list[str] = []
    matches = [r for r in results if r.status == "match"]
    excluded = [
        r
        for r in results
        if r.status in ("excluded", "hash_excluded", "expected_only_left", "expected_only_right")
    ]
    real_diffs = [r for r in results if r.status in REAL_DIFF_STATUSES]

    lines.append("# Filesystem Diff Report")
    lines.append("")
    lines.append(f"**{left_label}** vs **{right_label}**")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    total = len(results)
    lines.append("| | Count | % |")
    lines.append("|---|---|---|")
    lines.append(f"| Identical | {len(matches):,} | {100*len(matches)/total:.1f}% |")
    lines.append(f"| Excluded (expected) | {len(excluded):,} | {100*len(excluded)/total:.1f}% |")
    lines.append(
        f"| **Real differences** | **{len(real_diffs):,}** | **{100*len(real_diffs)/total:.1f}%** |"
    )
    lines.append(f"| Total | {total:,} | |")
    lines.append("")

    if not real_diffs:
        lines.append("**Clean diff** (no unexpected differences)")
        return "\n".join(lines)

    # Breakdown by status
    by_status: dict[str, list[DiffResult]] = defaultdict(list)
    for r in real_diffs:
        by_status[r.status].append(r)

    lines.append("## Real Differences")
    lines.append("")
    for status in [
        "only_left",
        "only_right",
        "type_changed",
        "content_changed",
        "link_changed",
        "metadata_changed",
    ]:
        items = by_status.get(status, [])
        if not items:
            continue
        label = {
            "only_left": f"Only in {left_label}",
            "only_right": f"Only in {right_label}",
            "type_changed": "Type changed",
            "content_changed": "Content changed (hash differs)",
            "link_changed": "Symlink target changed",
            "metadata_changed": "Metadata changed",
        }[status]
        lines.append(f"### {label} ({len(items):,})")
        lines.append("")

        # Group by category
        by_cat: dict[str, list[DiffResult]] = defaultdict(list)
        for r in items:
            by_cat[categorize_path(r.path)].append(r)

        for cat in sorted(by_cat):
            cat_items = sorted(by_cat[cat], key=lambda r: r.path)
            lines.append(f"**{cat}** ({len(cat_items)})")
            lines.append("")
            for r in cat_items[:100]:
                detail = f" — {r.details}" if r.details else ""
                lines.append(f"- `{r.path}`{detail}")
            if len(cat_items) > 100:
                lines.append(f"- *...and {len(cat_items)-100} more*")
            lines.append("")

    # Summary of excluded items
    if excluded:
        lines.append("## Excluded (expected differences)")
        lines.append("")
        excl_by_status: dict[str, int] = defaultdict(int)
        for r in excluded:
            excl_by_status[r.status] += 1
        for st, count in sorted(excl_by_status.items()):
            lines.append(f"- {st}: {count:,}")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two filesystem manifests (NDJSON or TSV)")
    parser.add_argument("left", help="Left manifest (e.g., live container)")
    parser.add_argument("right", help="Right manifest (e.g., built container)")
    parser.add_argument("--left-label", default="live")
    parser.add_argument("--right-label", default="built")
    parser.add_argument("--exclusions", help="JSON exclusion config file")
    parser.add_argument("-o", "--output", help="Output markdown report file")
    args = parser.parse_args()

    excl = load_exclusions(args.exclusions)

    print(f"Parsing {args.left}...", file=sys.stderr)
    left = parse_ndjson(args.left)
    print(f"  {len(left):,} entries", file=sys.stderr)

    print(f"Parsing {args.right}...", file=sys.stderr)
    right = parse_ndjson(args.right)
    print(f"  {len(right):,} entries", file=sys.stderr)

    print("Comparing...", file=sys.stderr)
    results = diff_manifests(left, right, excl)

    report = generate_report(results, args.left_label, args.right_label)
    real_diffs = sum(1 for r in results if r.status in REAL_DIFF_STATUSES)

    if args.output:
        Path(args.output).write_text(report)
        print(f"Report: {args.output} ({real_diffs:,} real differences)", file=sys.stderr)
    else:
        print(report)

    return 1 if real_diffs > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
