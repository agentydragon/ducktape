"""Where the obligations of a translation sit.

`translate --obligations <file>` writes every pair of conflicting units that the output runs in the opposite order from
the bundle, as `[runs first in the output, runs first in the bundle, reasons]` (`reasons` is the sum of 1 both touch the
world, 2 the later binds a name the earlier reads, 4 the earlier binds a name the later reads, 8 both bind a name).
This groups them by the directory of the output tree that holds each unit.

    python3 obligations_report.py <output.json> <cert.json> <obligations.jsonl> [--footprints <lower.jsonl>]
                                  [--depth N] [--top K]

For every directory (the first N segments of a module's directory, default 2): its units, how many of them touch the
world (with `--footprints`, the output of `lower`), the obligations between two of its units (inside) and between one
of its units and a unit elsewhere (crossing), and the same split by reason. A directory with no obligation at all can
be moved around freely. Then the units that sit in the most obligations.
"""

import argparse
import collections
import json
import pathlib

REASONS = (
    (1, "both touch the world"),
    (2, "later binds, earlier reads"),
    (4, "earlier binds, later reads"),
    (8, "both bind"),
)


def part_of(path: str, depth: int) -> str:
    directory = path.split("/")[:-1]
    return "/".join(directory[:depth]) or "."


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("output")
    ap.add_argument("cert")
    ap.add_argument("obligations")
    ap.add_argument("--footprints")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    modules = json.loads(pathlib.Path(args.output).read_text())["modules"]
    pairs = json.loads(pathlib.Path(args.cert).read_text())["pairs"]
    module_of = {b: m for m, _, b in pairs}
    part = {b: part_of(modules[m]["path"], args.depth) for b, m in module_of.items()}
    effect = {}
    if args.footprints:
        for i, line in enumerate(pathlib.Path(args.footprints).read_text().splitlines()):
            effect[i] = json.loads(line)["effect"]

    units = collections.Counter(part.values())
    effects = collections.Counter(part[b] for b in effect if effect[b]) if effect else {}
    inside = collections.Counter()
    crossing = collections.Counter()
    by_reason = collections.defaultdict(collections.Counter)
    degree = collections.Counter()
    total = 0
    total_reason = collections.Counter()
    for line in pathlib.Path(args.obligations).read_text().splitlines():
        a, b, r = json.loads(line)
        total += 1
        degree[a] += 1
        degree[b] += 1
        for bit, _ in REASONS:
            if r & bit:
                total_reason[bit] += 1
        pa, pb = part[a], part[b]
        if pa == pb:
            inside[pa] += 1
            for bit, _ in REASONS:
                if r & bit:
                    by_reason[pa][bit] += 1
        else:
            for p in (pa, pb):
                crossing[p] += 1
                for bit, _ in REASONS:
                    if r & bit:
                        by_reason[p][bit] += 1

    print(f"{total} obligations over {len(part)} units in {len(units)} directories (depth {args.depth})")
    print("by reason (a pair can have several): " + ", ".join(f"{total_reason[bit]} {what}" for bit, what in REASONS))
    print()
    free = sorted(p for p in units if inside[p] + crossing[p] == 0)
    print(
        f"{len(free)} directories hold {sum(units[p] for p in free)} units that are in no obligation at all, "
        f"{len([p for p in units if inside[p] == 0])} have none inside."
    )
    print()
    width = max(len(p) for p in units)
    print(
        f"{'directory':{width}}  {'units':>6} {'world':>6} {'inside':>9} {'crossing':>9}   reasons (world / later-binds / earlier-binds / both-bind)"
    )
    for p in sorted(units, key=lambda p: -(inside[p] + crossing[p])):
        r = by_reason[p]
        print(
            f"{p:{width}}  {units[p]:>6} {effects.get(p, 0) if effect else '-':>6} {inside[p]:>9} {crossing[p]:>9}   "
            + " / ".join(str(r[bit]) for bit, _ in REASONS)
        )
    print()
    print(f"the {args.top} units in the most obligations:")
    for u, d in degree.most_common(args.top):
        print(
            f"  unit {u:>5}  {d:>8} obligations  {'world' if effect.get(u) else 'pure ' if effect else '     '}  {modules[module_of[u]]['path']}"
        )


if __name__ == "__main__":
    main()
