"""Extract one `CustomResourceDefinition` document from a multi-document CRD bundle.

`cdk8s import` only ingests a single CRD per invocation; some projects (external-secrets)
publish their CRDs as one bundled multi-document YAML rather than per-kind files.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="metadata.name of the CustomResourceDefinition to extract")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()

    for doc in yaml.safe_load_all(args.bundle.read_text()):
        if doc and doc.get("kind") == "CustomResourceDefinition" and doc["metadata"]["name"] == args.name:
            args.out.write_text(yaml.dump(doc))
            return
    raise SystemExit(f"No CustomResourceDefinition named {args.name!r} found in {args.bundle}")


if __name__ == "__main__":
    main()
