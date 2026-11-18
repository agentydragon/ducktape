"""Command-line inputs and matrix step outputs shared by the publication planners."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from devinfra.ci import publication_evidence


def add_evidence_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--invocations", default="", help="comma-separated Bazel CI build invocation IDs")
    parser.add_argument("--test-invocation-id", default="", help="exact Bazel CI test invocation ID")
    parser.add_argument("--test-source-commit", default="", help="source SHA verified by Bazel CI checkout")
    parser.add_argument("--rbe-container-image", default="", help="publication test RBE image override, if any")


def load_evidence(args: argparse.Namespace) -> publication_evidence.PublicationEvidence:
    return publication_evidence.load(
        [value for value in args.invocations.split(",") if value],
        test_invocation_id=args.test_invocation_id,
        source_commit=args.test_source_commit,
        rbe_container_image=args.rbe_container_image,
    )


def write_matrix(rows: list[dict[str, str]]) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        matrix = json.dumps({"include": rows})
        with Path(path).open("a", encoding="utf-8") as output:
            output.write(f"matrix={matrix}\ncount={len(rows)}\n")
