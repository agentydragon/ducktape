"""Bound what scoping the escape pool would leave in each `callback_pool_limit` pool.

Reads the logs `probe.patch` writes under `DEBUNDLE_HAVOC_PROBE_DIR` and the run's
`dependency_graph.json` files. Usage: `pool_regions.py <probe dir> <graph file or tree dir>`.
"""

import json
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

LIMIT = 64
ALL = "all"
GENERAL = {"general", "op", "other", "unlabelled"}
VARIANTS = ("per_chunk", "region_untagged", "region_tagged")


def static_closure(modules: dict, start: str) -> set[str]:
    seen, todo = {start}, [start]
    while todo:
        for target, _ in modules.get(todo.pop(), {}).get("imports", {}).values():
            if target not in seen:
                seen.add(target)
                todo.append(target)
    return seen


def export_regions(module: dict) -> dict[str, frozenset[int]]:
    """Export name -> the non-function source units its read/write closure reaches."""
    declares = {name: unit["ordinal"] for unit in module["units"] for name in unit["declared"]}
    adjacency = defaultdict(set)
    for unit in module["units"]:
        for name in unit["reads"] + unit["rebinds"] + unit["member_write_roots"]:
            if name in declares and declares[name] != unit["ordinal"]:
                adjacency[unit["ordinal"]].add(declares[name])
    stateful = {unit["ordinal"] for unit in module["units"] if unit["kind"] not in ("FnDecl", "Export", "Import")}
    regions = {}
    for export, local in module["exports"].items():
        if local not in declares:
            continue
        seen, todo = {declares[local]}, deque([declares[local]])
        while todo:
            for target in adjacency[todo.popleft()] - seen:
                seen.add(target)
                todo.append(target)
        regions[export] = frozenset(seen & stateful)
    return regions


class Scopes:
    def __init__(self, modules: dict) -> None:
        self.modules = modules
        self.regions = {key: export_regions(module) for key, module in modules.items() if module["units"]}

    def target(self, importer: dict, label: dict, *, tagged: bool) -> tuple[str, str] | None:
        """The import-acyclic provider export a call label resolves to."""
        if label.get("site") or label["shape"] in GENERAL or (label["shape"] == "chain" and not tagged):
            return None
        root = label["resolved"] if tagged else label["root"]
        if root not in importer["imports"]:
            return None
        provider, export = importer["imports"][root]
        if importer["module"] in static_closure(self.modules, provider):
            return None
        return provider, export

    def scope(self, importer: dict | None, label: dict, variant: str):
        target = importer and self.target(importer, label, tagged=variant != "region_untagged")
        if not target:
            return ALL
        provider, export = target
        if variant == "per_chunk":
            return ("chunk", frozenset(static_closure(self.modules, provider)))
        region = self.regions.get(provider, {}).get(export)
        return ALL if region is None else ("region", provider, region)


def overlaps(left, right) -> bool:
    if ALL in (left, right):
        return True
    if left[0] == "chunk":
        return bool(left[1] & right[1])
    return left[1] == right[1] and bool(left[2] & right[2])


def out_edges(graph_path: Path, source_of: dict) -> Counter:
    graphs = [graph_path] if graph_path.is_file() else sorted(graph_path.rglob("dependency_graph.json"))
    counts: Counter[tuple[str | None, int]] = Counter()
    for path in graphs:
        graph = json.loads(path.read_text())
        source = source_of.get(graph.get("chunk_id"))
        for edge in graph["edges"]:
            if edge.get("constrains_init_order") and edge["source"].startswith("unit:"):
                counts[(source, int(edge["source"].removeprefix("unit:")))] += 1
    return counts


def main() -> None:
    probe, graph_path = Path(sys.argv[1]), Path(sys.argv[2])
    modules = {}
    for path in probe.glob("module.*.json"):
        module = json.loads(path.read_text())
        modules[module["module"]] = module
    by_source = {module["source"]: module for module in modules.values() if module.get("source")}
    scopes = Scopes(modules)
    kept_sizes: dict[str, list[int]] = {variant: [] for variant in VARIANTS}
    statement_under: dict[str, defaultdict[tuple[str, int], bool]] = {
        variant: defaultdict(lambda: True) for variant in VARIANTS
    }
    no_export_under: defaultdict[tuple[str, int], bool] = defaultdict(lambda: True)
    first_route = {}
    events = 0
    for path in probe.glob("pool.*.jsonl"):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            events += 1
            importer = by_source.get(event["chunk"])
            statement = (event["chunk"], event["statement"])
            call = event["call"] or {"shape": "op"}
            members = event["members"]
            no_export_under[statement] &= sum(not member["export"] for member in members) <= LIMIT
            for member in members:
                if member["escaped"] and not member["export"] and member["key"] is not None:
                    sink = (member["sinks"] or [{"shape": "unlabelled"}])[0]
                    first_route[(event["chunk"], member["kind"], member["key"])] = sink.get("site") or (
                        "call_root_import"
                        if importer and sink.get("root") in importer["imports"]
                        else f"call_root_other_{sink['shape']}"
                    )
            for variant in VARIANTS:
                scope = scopes.scope(importer, call, variant)
                kept = sum(
                    member["export"]
                    or not member["escaped"]
                    or any(
                        overlaps(scope, scopes.scope(importer, sink, variant))
                        for sink in member["sinks"] or [{"shape": "unlabelled"}]
                    )
                    for member in members
                )
                kept_sizes[variant].append(kept)
                statement_under[variant][statement] &= kept <= LIMIT
    edges = out_edges(graph_path, {key: module.get("source") for key, module in modules.items()})
    report = {
        "pool_events": events,
        "statements": len(no_export_under),
        "statement_out_edges": sum(edges[statement] for statement in no_export_under),
        "distinct_escaped_by_first_route": dict(Counter(first_route.values()).most_common()),
        "without_export_callbacks": {
            "statements_all_events_under": sum(no_export_under.values()),
            "their_out_edges": sum(edges[s] for s, under in no_export_under.items() if under),
        },
    }
    for variant in VARIANTS:
        sizes = sorted(kept_sizes[variant])
        under = [statement for statement, ok in statement_under[variant].items() if ok]
        report[variant] = {
            "events_under": sum(size <= LIMIT for size in sizes),
            "median_pool": sizes[len(sizes) // 2],
            "max_pool": sizes[-1],
            "statements_all_events_under": len(under),
            "their_out_edges": sum(edges[statement] for statement in under),
        }
    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
