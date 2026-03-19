"""Visualize the Flux Kustomization dependency DAG as an interactive D3.js HTML page.

Usage: bazel run //cluster/scripts/visualize_dag [-- --output /path/to/dag.html]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import networkx as nx

from cluster.validation.cluster import _K8S_SUBPATH, parse_cluster
from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_working_directory, get_build_workspace_directory

_DATA_PLACEHOLDER = "/*GRAPH_DATA*/null"
_BUNDLE_PLACEHOLDER = "/*BUNDLE*/"


def _build_graph_json(g: nx.DiGraph) -> str:
    """Serialize the dependency graph as JSON for the D3 template."""
    nodes = [{"id": name} for name in g.nodes]
    links = [{"source": u, "target": v} for u, v in g.edges]
    return json.dumps({"nodes": nodes, "links": links})


def main() -> int:
    parser = argparse.ArgumentParser(description="Flux Kustomization DAG → interactive HTML")
    parser.add_argument("--output", "-o", type=Path, default=Path("flux-dag.html"))
    parser.add_argument("--include-suspended", action="store_true", help="Include suspended kustomizations")
    args = parser.parse_args()

    k8s_dir = get_build_workspace_directory() / _K8S_SUBPATH
    print(f"Parsing {k8s_dir}...", file=sys.stderr)
    cluster = parse_cluster(k8s_dir)
    g = cluster.graph
    if not args.include_suspended:
        suspended = {name for name, ks in cluster.flux_kustomizations.items() if ks.spec.suspend}
        if suspended:
            g = g.copy()
            g.remove_nodes_from(suspended)
            print(f"Excluded {len(suspended)} suspended kustomizations", file=sys.stderr)
    print(f"{g.number_of_nodes()} nodes, {g.number_of_edges()} edges", file=sys.stderr)

    graph_json = _build_graph_json(g)
    template = get_required_path("_main/cluster/scripts/visualize_dag/template.html").read_text()
    bundle = get_required_path("_main/cluster/scripts/visualize_dag/dag.bundle.js").read_text()

    html = template.replace(_DATA_PLACEHOLDER, graph_json).replace(_BUNDLE_PLACEHOLDER, bundle)

    out = args.output if args.output.is_absolute() else get_build_working_directory() / args.output
    out.write_text(html)
    print(f"Written to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
