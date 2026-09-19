"""Extract one `CustomResourceDefinition` document from a multi-document CRD bundle.

`cdk8s import` only ingests a single CRD per invocation; some projects (external-secrets)
publish their CRDs as one bundled multi-document YAML rather than per-kind files.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


def _remove_path(document: Any, path: str) -> None:
    """Remove one dotted YAML path; numeric segments index arrays."""
    parts = path.split(".")
    if not all(parts):
        raise ValueError(f"Invalid empty segment in path {path!r}")

    node = document
    try:
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        final = parts[-1]
        if isinstance(node, list):
            del node[int(final)]
        else:
            del node[final]
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot remove missing YAML path {path!r}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="metadata.name of the CustomResourceDefinition to extract")
    parser.add_argument(
        "--version",
        help=(
            "Keep only this spec.versions[].name entry, dropping the rest. `cdk8s import` generates "
            "bindings for whichever version a multi-version CRD lists last, not necessarily the "
            "served+storage one -- pass this to pin it explicitly."
        ),
    )
    parser.add_argument(
        "--remove-path",
        action="append",
        default=[],
        help="Dotted YAML path to remove from the extracted document; numeric segments index arrays.",
    )
    parser.add_argument("bundle", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()

    for doc in yaml.safe_load_all(args.bundle.read_text()):
        if doc and doc.get("kind") == "CustomResourceDefinition" and doc["metadata"]["name"] == args.name:
            if args.version is not None:
                versions = [v for v in doc["spec"]["versions"] if v["name"] == args.version]
                if not versions:
                    raise SystemExit(f"No version {args.version!r} found on CRD {args.name!r}")
                doc["spec"]["versions"] = versions
            for path in args.remove_path:
                try:
                    _remove_path(doc, path)
                except ValueError as exc:
                    raise SystemExit(str(exc)) from exc
            args.out.write_text(yaml.dump(doc))
            return
    raise SystemExit(f"No CustomResourceDefinition named {args.name!r} found in {args.bundle}")


if __name__ == "__main__":
    main()
