from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from tana.domain.constants import MIN_TUPLE_CHILDREN, SUPERTAG_KEY_ID
from tana.domain.nodes import DOC_CLASS, BaseNode, TupleNode, UnknownNode
from tana.domain.types import NodeId
from tana.export.convert import RenderContext
from tana.graph.graph import TanaGraph
from tana.graph.wrappers import is_wrapper

PRETTY_PRINT_LIMIT = 10

# Constants from convert.py
_SUPERTAG_KEY_ID = NodeId(SUPERTAG_KEY_ID)


class TrackingGraph(TanaGraph):
    """A TanaGraph that tracks which nodes are accessed."""

    def __init__(self, mapping: dict[NodeId, BaseNode]):
        super().__init__(mapping)
        self.accessed_nodes: set[NodeId] = set()
        self.tracking_enabled: bool = True

    def __getitem__(self, node_id: NodeId) -> BaseNode:
        if self.tracking_enabled:
            self.accessed_nodes.add(node_id)
        return super().__getitem__(node_id)

    def get(self, node_id: NodeId, default=None):
        if node_id in self and self.tracking_enabled:
            self.accessed_nodes.add(node_id)
        return super().get(node_id, default)


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
            child_node_id = NodeId(child_id)
            if child_node_id not in store:
                continue
            child = store[child_node_id]
            if (
                isinstance(child, TupleNode)
                and len(child.children) >= MIN_TUPLE_CHILDREN
                and child.children[0] == _SUPERTAG_KEY_ID
            ):
                dependencies.add(child.id)
                for tag_id in child.children[1:]:
                    tag_node_id = NodeId(tag_id)
                    if tag_node_id in store:
                        dependencies.add(tag_node_id)
                        tag_node = store[tag_node_id]
                        if tag_node.props.meta_node_id:
                            dependencies.add(tag_node.props.meta_node_id)
                            to_process.add(tag_node.props.meta_node_id)

        if node.props.owner_id and node.props.owner_id in store:
            owner = store[node.props.owner_id]
            if is_wrapper(owner):
                dependencies.add(owner.id)
                to_process.add(owner.id)

    return dependencies


class TrackingRenderContext(RenderContext):
    """A RenderContext that ensures node access is tracked."""

    def __init__(self, store: TrackingGraph, style: str):
        super().__init__(store, style)
        self.tracking_store = store

    def render_node(self, n: BaseNode):
        # Make sure the node itself is tracked
        self.tracking_store.accessed_nodes.add(n.id)
        yield from super().render_node(n)


def export_node_with_tracking(store: TrackingGraph, node_id: NodeId) -> tuple[str, set[NodeId]]:
    """
    Export a single node as TanaPaste and return the export along with touched node IDs.

    Returns:
        tuple: (tanapaste_output, set_of_touched_node_ids)
    """
    # Get the target node
    if node_id not in store:
        raise ValueError(f"Node {node_id} not found in store")

    node = store[node_id]

    # Clear accessed nodes to start fresh
    store.accessed_nodes.clear()

    # Export the node using tracking context
    lines = ["%%tana%%"]
    ctx = TrackingRenderContext(store, "tana")
    lines.extend(ctx.render_node(node))

    tanapaste = "\n".join(lines).rstrip() + "\n\n"

    # Get all accessed nodes from the export
    export_nodes = store.accessed_nodes.copy()

    # Disable tracking while collecting dependencies to avoid cascading
    store.tracking_enabled = False
    supertag_deps = collect_supertag_dependencies(store, export_nodes)
    store.tracking_enabled = True

    # Combine both sets
    all_accessed = export_nodes | supertag_deps

    # Return the export and all accessed nodes
    return tanapaste, all_accessed


def create_subset_json(original_data: dict[str, Any], touched_nodes: set[NodeId]) -> dict[str, Any]:
    filtered_docs = [doc for doc in original_data["docs"] if NodeId(str(doc["id"])) in touched_nodes]

    return {"formatVersion": original_data.get("formatVersion", 1), "docs": filtered_docs}


def main():
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

    # Create tracking store with proper node types

    def _make_node(raw: dict[str, Any]) -> BaseNode:
        node_model = DOC_CLASS.get(raw["props"].get("_docType"), UnknownNode)
        # All DOC_CLASS values are BaseNode subclasses; model_validate returns the specific type
        return cast(BaseNode, node_model.model_validate(raw))

    tracking_store = TrackingGraph({NodeId(str(doc["id"])): _make_node(doc) for doc in original_data["docs"]})

    # Export the node with tracking; supertags are resolved via TanaGraph indexes
    try:
        target_id = NodeId(args.node_id)
        tanapaste_output, touched_nodes = export_node_with_tracking(tracking_store, target_id)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

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
    for node_id in sorted(touched_nodes)[:PRETTY_PRINT_LIMIT]:
        node = tracking_store.get(node_id)
        if node and node.name:
            node_names.append(f"  - {node.name[:50]}... ({node_id})")
        else:
            node_names.append(f"  - <unnamed> ({node_id})")

    print("\n".join(node_names))
    if len(touched_nodes) > PRETTY_PRINT_LIMIT:
        print(f"  ... and {len(touched_nodes) - PRETTY_PRINT_LIMIT} more nodes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
