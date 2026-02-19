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
    bazel run //claude_web_env/tools:diff_manifests -- live.ndjson built.ndjson
    bazel run //claude_web_env/tools:diff_manifests -- live.ndjson built.ndjson --exclusions exclusions.yaml
    bazel run //claude_web_env/tools:diff_manifests -- live.ndjson built.ndjson -o report.md
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel

from claude_web_env.tools.manifest import Entry, Exclusions, load_exclusions, parse_ndjson

REAL_DIFF_STATUSES = {"only_left", "only_right", "type_changed", "content_changed", "link_changed", "metadata_changed"}


class DiffResult(BaseModel):
    path: str
    status: str  # match, excluded, hash_excluded, only_left, only_right,
    # expected_only_left, expected_only_right,
    # type_changed, content_changed, link_changed, metadata_changed
    details: str = ""
    left: Entry | None = None
    right: Entry | None = None


PatternHits = dict[tuple[str, str], int]


def diff_manifests(
    left: dict[str, Entry], right: dict[str, Entry], excl: Exclusions
) -> tuple[list[DiffResult], PatternHits]:
    all_paths = sorted(set(left) | set(right))
    results: list[DiffResult] = []
    hits: PatternHits = defaultdict(int)

    def record(category: str, pattern: str) -> None:
        hits[(category, pattern)] += 1

    for path in all_paths:
        skip_pat = excl.matching_skip(path)
        if skip_pat is not None:
            record("skip_paths", skip_pat)
            results.append(DiffResult(path=path, status="excluded"))
            continue

        le = left.get(path)
        re = right.get(path)

        if le and not re:
            live_match = excl.matching_only_in_live(path)
            vol_pat = excl.matching_volatile(path)
            if live_match is not None:
                record(*live_match)
                results.append(DiffResult(path=path, status="expected_only_left", left=le))
            elif vol_pat is not None:
                record("volatile_paths", vol_pat)
                results.append(DiffResult(path=path, status="expected_only_left", left=le))
            else:
                results.append(DiffResult(path=path, status="only_left", left=le))
            continue
        if re and not le:
            built_pat = excl.matching_only_in_built(path)
            vol_pat = excl.matching_volatile(path)
            if built_pat is not None:
                record("only_in_built", built_pat)
                results.append(DiffResult(path=path, status="expected_only_right", right=re))
            elif vol_pat is not None:
                record("volatile_paths", vol_pat)
                results.append(DiffResult(path=path, status="expected_only_right", right=re))
            else:
                results.append(DiffResult(path=path, status="only_right", right=re))
            continue

        assert le
        assert re

        vol_pat = excl.matching_volatile(path)
        volatile = vol_pat is not None

        if le.type != re.type:
            if volatile:
                assert vol_pat is not None
                record("volatile_paths", vol_pat)
                results.append(DiffResult(path=path, status="hash_excluded", details="volatile", left=le, right=re))
                continue
            results.append(
                DiffResult(path=path, status="type_changed", details=f"{le.type}->{re.type}", left=le, right=re)
            )
            continue

        if le.type == "l" and le.link_target != re.link_target:
            if volatile:
                assert vol_pat is not None
                record("volatile_paths", vol_pat)
                results.append(DiffResult(path=path, status="hash_excluded", details="volatile", left=le, right=re))
                continue
            results.append(
                DiffResult(
                    path=path, status="link_changed", details=f"{le.link_target}->{re.link_target}", left=le, right=re
                )
            )
            continue

        if le.type == "f" and le.sha256 and re.sha256 and le.sha256 != re.sha256:
            hash_pat = excl.matching_hash_ok(path)
            if hash_pat is not None:
                record("hash_may_differ", hash_pat)
                results.append(
                    DiffResult(path=path, status="hash_excluded", details="hash differs (expected)", left=le, right=re)
                )
                continue
            if volatile:
                assert vol_pat is not None
                record("volatile_paths", vol_pat)
                results.append(
                    DiffResult(path=path, status="hash_excluded", details="hash differs (expected)", left=le, right=re)
                )
                continue
            results.append(
                DiffResult(path=path, status="content_changed", details=f"size {le.size}->{re.size}", left=le, right=re)
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
                assert vol_pat is not None
                record("volatile_paths", vol_pat)
                results.append(DiffResult(path=path, status="hash_excluded", details="volatile", left=le, right=re))
                continue
            results.append(
                DiffResult(path=path, status="metadata_changed", details=", ".join(changes), left=le, right=re)
            )
            continue

        results.append(DiffResult(path=path, status="match", left=le, right=re))

    return results, hits


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


def generate_report(
    results: list[DiffResult],
    left_label: str,
    right_label: str,
    pattern_hits: PatternHits | None = None,
    excl: Exclusions | None = None,
) -> str:
    lines: list[str] = []
    matches = [r for r in results if r.status == "match"]
    excluded = [
        r for r in results if r.status in ("excluded", "hash_excluded", "expected_only_left", "expected_only_right")
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
    lines.append(f"| Identical | {len(matches):,} | {100 * len(matches) / total:.1f}% |")
    lines.append(f"| Excluded (expected) | {len(excluded):,} | {100 * len(excluded) / total:.1f}% |")
    lines.append(f"| **Real differences** | **{len(real_diffs):,}** | **{100 * len(real_diffs) / total:.1f}%** |")
    lines.append(f"| Total | {total:,} | |")
    lines.append("")

    if not real_diffs:
        lines.append("**Clean diff** (no unexpected differences)")
    else:
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
                    lines.append(f"- *...and {len(cat_items) - 100} more*")
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

    # Pattern utilization
    if pattern_hits is not None and excl is not None:
        lines.append(_generate_pattern_section(pattern_hits, excl, len(excluded), len(real_diffs)))

    return "\n".join(lines)


def _generate_pattern_section(hits: PatternHits, excl: Exclusions, total_excluded: int, total_real_diffs: int) -> str:
    all_pats = excl.all_patterns()
    total_patterns = len(all_pats)
    unused = [p for p in all_pats if hits.get(p, 0) == 0]
    total_hits = sum(hits.values())

    lines: list[str] = []
    lines.append("## Exclusion Pattern Utilization")
    lines.append("")
    lines.append(
        f"{total_patterns} patterns excluded {total_excluded:,} paths "
        f"({total_hits:,} attributed to specific patterns). "
        f"{len(unused)} patterns matched 0 paths."
    )
    if total_real_diffs > 0:
        lines.append(f"Ratio: {total_patterns / total_real_diffs:.1f}x patterns per real diff.")
    lines.append("")

    # Group by category, preserving YAML order
    categories = [
        "skip_paths",
        "volatile_paths",
        "hash_may_differ",
        "only_in_live",
        "session_hook_artifacts",
        "only_in_built",
    ]
    for cat in categories:
        cat_pats = [(pat, hits.get((cat, pat), 0)) for c, pat in all_pats if c == cat]
        if not cat_pats:
            continue
        cat_total = sum(count for _, count in cat_pats)
        cat_unused = sum(1 for _, count in cat_pats if count == 0)
        lines.append(f"### `{cat}` ({len(cat_pats)} patterns, {cat_total:,} hits, {cat_unused} unused)")
        lines.append("")
        lines.append("| Hits | Pattern |")
        lines.append("|-----:|---------|")
        # Sort: nonzero descending, then zero-hit patterns
        for pat, count in sorted(cat_pats, key=lambda x: (-x[1], x[0])):
            marker = " **UNUSED**" if count == 0 else ""
            lines.append(f"| {count:,} | `{pat}`{marker} |")
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
    results, pattern_hits = diff_manifests(left, right, excl)

    report = generate_report(results, args.left_label, args.right_label, pattern_hits, excl)
    real_diffs = sum(1 for r in results if r.status in REAL_DIFF_STATUSES)

    if args.output:
        Path(args.output).write_text(report)
        print(f"Report: {args.output} ({real_diffs:,} real differences)", file=sys.stderr)
    else:
        print(report)

    return 1 if real_diffs > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
