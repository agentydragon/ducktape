"""Write metadata for a GitHub release artifact.

The asset named after the package is the release's `binary`; every other asset
is a sidecar that binary needs at run time, listed by file name under
`sidecars` (`debundle` and its `selector_cpsat_solver`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path


def _sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def write_release_metadata(
    *, output: Path, package: str, tag: str, git_commit: str, assets: Sequence[Path], platform: str | None
) -> None:
    binaries = [asset for asset in assets if asset.name == package]
    if len(binaries) != 1:
        raise ValueError(f"release {package} needs exactly one asset named {package}, got {[a.name for a in assets]}")
    [binary] = binaries
    metadata: dict[str, str | dict[str, str]] = {
        "binary": binary.name,
        "git_commit": git_commit,
        "package": package,
        "sha256": _sha256(binary),
        "tag": tag,
    }
    if sidecars := {asset.name: _sha256(asset) for asset in assets if asset != binary}:
        metadata["sidecars"] = sidecars
    if platform is not None:
        metadata["platform"] = platform
    output.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--platform")
    parser.add_argument("assets", type=Path, nargs="+")
    args = parser.parse_args()

    write_release_metadata(
        output=args.output,
        package=args.package,
        tag=args.tag,
        git_commit=args.git_commit,
        assets=args.assets,
        platform=args.platform,
    )


if __name__ == "__main__":
    main()
