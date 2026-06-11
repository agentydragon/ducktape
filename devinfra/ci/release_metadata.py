"""Write metadata for a GitHub release artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def write_release_metadata(
    *, output: Path, package: str, tag: str, git_commit: str, binary: str, sha256: str, platform: str | None
) -> None:
    metadata = {"binary": binary, "git_commit": git_commit, "package": package, "sha256": sha256, "tag": tag}
    if platform is not None:
        metadata["platform"] = platform
    output.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--platform")
    args = parser.parse_args()

    write_release_metadata(
        output=args.output,
        package=args.package,
        tag=args.tag,
        git_commit=args.git_commit,
        binary=args.binary,
        sha256=args.sha256,
        platform=args.platform,
    )


if __name__ == "__main__":
    main()
