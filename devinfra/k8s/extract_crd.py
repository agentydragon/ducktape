"""Extract one `CustomResourceDefinition` from an upstream CRD source for `cdk8s import`.

`cdk8s import` only ingests a single CRD per invocation. Sources are a multi-document
YAML bundle (external-secrets publishes its CRDs as one file), or, with `--go-key`, a
generated Go file whose map literal embeds each CRD as a raw string: KubeVirt and CDI
publish no YAML for the CRDs their operators create at runtime.
"""

from __future__ import annotations

import argparse
import re
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


def go_raw_string(source: str, key: str) -> str:
    """The raw-string value of `key` in a generated Go `map[string]string` literal.

    A Go raw string cannot contain a backtick, so the value ends at the next one.
    """
    match = re.search(rf'^\t"{re.escape(key)}": `([^`]*)`', source, re.MULTILINE)
    if match is None:
        raise ValueError(f"No raw-string map entry {key!r} found")
    return match.group(1)


def wrap_schema(schema_entry: dict[str, Any], *, name: str, kind: str, version: str) -> dict[str, Any]:
    """A minimal namespaced CRD serving `schema_entry` (a mapping with `openAPIV3Schema`).

    Group and plural come from `name` (`<plural>.<group>`), as they do on any CRD.
    """
    plural, _, group = name.partition(".")
    if not group:
        raise ValueError(f"CRD name {name!r} is not <plural>.<group>")
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": name},
        "spec": {
            "group": group,
            "names": {"kind": kind, "listKind": f"{kind}List", "plural": plural, "singular": kind.lower()},
            "scope": "Namespaced",
            "versions": [{"name": version, "served": True, "storage": True, "schema": schema_entry}],
        },
    }


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
        "--go-key", help="Read the source as a generated Go file and take the raw-string map value under this key."
    )
    parser.add_argument(
        "--wrap-kind",
        help=(
            "The --go-key value is only a schema entry (`openAPIV3Schema: ...`): wrap it in a namespaced "
            "CRD of this kind, named --name, serving --version."
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
    if args.wrap_kind is not None and (args.go_key is None or args.version is None):
        parser.error("--wrap-kind requires --go-key and --version")

    text = args.bundle.read_text()
    if args.go_key is not None:
        text = go_raw_string(text, args.go_key)
    docs = (
        [wrap_schema(yaml.safe_load(text), name=args.name, kind=args.wrap_kind, version=args.version)]
        if args.wrap_kind is not None
        else yaml.safe_load_all(text)
    )
    for doc in docs:
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
