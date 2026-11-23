"""Check that Bazel and the Nix-provided rustfmt use the same Rust release."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def check_versions(module_text: str, rustfmt_source: str) -> None:
    toolchains = [
        node
        for node in ast.walk(ast.parse(module_text))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "rust"
        and node.func.attr == "toolchain"
    ]
    if len(toolchains) != 1:
        raise ValueError("expected exactly one rust.toolchain call in MODULE.bazel")
    versions = [ast.literal_eval(arg.value) for arg in toolchains[0].keywords if arg.arg == "versions"]
    if len(versions) != 1 or not isinstance(versions[0], list) or len(versions[0]) != 1:
        raise ValueError("expected exactly one literal Bazel Rust toolchain version")
    bazel_version = versions[0][0]

    name_parts = rustfmt_source.split("-")
    if (
        len(name_parts) < 3
        or name_parts[0] != "rustfmt"
        or not all(part.isdecimal() for part in name_parts[1].split("."))
        or len(name_parts[1].split(".")) != 3
    ):
        raise ValueError(f"unexpected Fenix rustfmt source name: {rustfmt_source}")
    fenix_version = name_parts[1]
    if bazel_version != fenix_version:
        raise ValueError(f"Bazel Rust {bazel_version} differs from Fenix rustfmt {fenix_version}")


if __name__ == "__main__":
    try:
        check_versions(Path("MODULE.bazel").read_text(), sys.argv[1])
    except (IndexError, ValueError, SyntaxError) as exc:
        sys.exit(str(exc))
