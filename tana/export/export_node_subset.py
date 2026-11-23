from __future__ import annotations

import argparse
import heapq
import json
from pathlib import Path
from typing import Any

from tana.domain.nodes import MIN_TUPLE_CHILDREN, BaseNode, TupleNode
from tana.domain.system_nodes import SUPERTAG_KEY_ID
from tana.domain.types import NodeId
from tana.export.convert import RenderContext
from tana.graph.graph import TanaGraph
from tana.graph.wrappers import is_wrapper

PRETTY_PRINT_LIMIT = 10


class TrackingGraph(TanaGraph):
    """A TanaGraph that tracks which nodes are accessed."""

    accessed_nodes: set[NodeId] | None = None

    def __getitem__(self, node_id: NodeId) -> BaseNode:
        node = super().__getitem__(node_id)
        if self.accessed_nodes is not None:
            self.accessed_nodes.add(node_id)
        return node


def collect_supertag_dependencies(store: TanaGraph, node_ids: set[NodeId]) -> set[NodeId]:
    """Collect nodes required to resolve supertags for the given nodes."""

    dependencies: set[NodeId] = set()
    to_process: set[NodeId] = set(node_ids)
    processed: set[NodeId] = set()

    while to_process:
        current_id = to_process.pop()
        if current_id in processed:
            continue
        processed.add(current_id)

        if current_id not in store:
            continue

        node = store[current_id]

        if node.props.meta_node_id and node.props.meta_node_id in store:
            dependencies.add(node.props.meta_node_id)
            to_process.add(node.props.meta_node_id)

        for child_id in node.children:
            if child_id not in store:
                continue
            child = store[child_id]
            if (
                isinstance(child, TupleNode)
                and len(child.children) >= MIN_TUPLE_CHILDREN
                and child.children[0] == SUPERTAG_KEY_ID
            ):
                dependencies.add(child.id)
                # The graph needs the key document to index supertags.
                if SUPERTAG_KEY_ID in store:
                    dependencies.add(SUPERTAG_KEY_ID)
                for tag_id in child.children[1:]:
                    if tag_id in store:
                        dependencies.add(tag_id)
                        tag_node = store[tag_id]
                        if tag_node.props.meta_node_id:
                            dependencies.add(tag_node.props.meta_node_id)
                            to_process.add(tag_node.props.meta_node_id)

        if node.props.owner_id and node.props.owner_id in store:
            owner = store[node.props.owner_id]
            if is_wrapper(owner):
                dependencies.add(owner.id)
                to_process.add(owner.id)

    return dependencies


def export_node_with_tracking(store: TrackingGraph, node_id: NodeId) -> tuple[str, set[NodeId]]:
    """Return (TanaPaste, accessed IDs plus supertag dependencies) for one node."""
    node = store.get(node_id)
    if node is None:
        raise ValueError(f"Node {node_id} not found in store")

    accessed = {node_id}
    store.accessed_nodes = accessed
    try:
        lines = ["%%tana%%", *RenderContext(store, "tana").render_node(node)]
    finally:
        store.accessed_nodes = None

    tanapaste = "\n".join(lines).rstrip() + "\n\n"
    return tanapaste, accessed | collect_supertag_dependencies(store, accessed)


def create_subset_json(original_data: dict[str, Any], touched_nodes: set[NodeId]) -> dict[str, Any]:
    filtered_docs = [doc for doc in original_data["docs"] if NodeId(str(doc["id"])) in touched_nodes]

    return {"formatVersion": original_data.get("formatVersion", 1), "docs": filtered_docs}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a single Tana node and create a subset JSON with only touched nodes"
    )
    parser.add_argument("json_file", type=Path, help="Path to Tana JSON export file")
    parser.add_argument("node_id", help="ID of the node to export")
    parser.add_argument(
        "-o",
        "--output-prefix",
        help="Prefix for output files (default: based on input filename and node ID)",
        type=Path,
        default=None,
    )
    parser.add_argument("--no-tanapaste", action="store_true", help="Skip creating the TanaPaste export file")
    parser.add_argument("--no-subset", action="store_true", help="Skip creating the subset JSON file")

    args = parser.parse_args()

    # Load the original JSON
    json_path = args.json_file
    with json_path.open(encoding="utf-8") as f:
        original_data = json.load(f)

    tracking_store = TrackingGraph.from_documents(original_data["docs"])

    # Export the node with tracking; supertags are resolved via TanaGraph indexes
    try:
        target_id = NodeId(args.node_id)
        tanapaste_output, touched_nodes = export_node_with_tracking(tracking_store, target_id)
    except ValueError as e:
        raise SystemExit(f"Error: {e}") from e

    # Determine output prefix
    if args.output_prefix:
        output_prefix = args.output_prefix
    else:
        output_prefix = json_path.with_suffix("") / f"node_{args.node_id}"
        output_prefix.parent.mkdir(exist_ok=True)

    # Write TanaPaste export
    if not args.no_tanapaste:
        tanapaste_path = output_prefix.with_suffix(".tanapaste.txt")
        tanapaste_path.write_text(tanapaste_output, encoding="utf-8")
        print(f"✅ TanaPaste export → {tanapaste_path}")

    # Create and write subset JSON
    if not args.no_subset:
        subset_data = create_subset_json(original_data, touched_nodes)
        subset_path = output_prefix.with_suffix(".subset.json")

        with subset_path.open("w", encoding="utf-8") as f:
            json.dump(subset_data, f, indent=2, ensure_ascii=False)

        print(f"✅ Subset JSON → {subset_path}")
        print(f"   Original nodes: {len(original_data['docs'])}")
        print(f"   Subset nodes: {len(subset_data['docs'])} (touched during export)")

    # Print summary of touched nodes
    print(f"\n📊 Export touched {len(touched_nodes)} nodes:")

    # Show first few node names if available
    node_names = []
    for node_id in heapq.nsmallest(PRETTY_PRINT_LIMIT, touched_nodes):
        node = tracking_store.get(node_id)
        if node and node.name:
            node_names.append(f"  - {node.name[:50]}... ({node_id})")
        else:
            node_names.append(f"  - <unnamed> ({node_id})")

    print("\n".join(node_names))
    if len(touched_nodes) > PRETTY_PRINT_LIMIT:
        print(f"  ... and {len(touched_nodes) - PRETTY_PRINT_LIMIT} more nodes")


if __name__ == "__main__":
    main()
