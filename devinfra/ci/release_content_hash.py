"""Compute the content identity for a GitHub release's complete asset set."""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Sequence
from pathlib import Path


def release_content_hash(artifacts: Sequence[Path]) -> str:
    """Return a stable digest that changes when any release asset changes."""
    if not artifacts:
        raise ValueError("at least one artifact is required")

    if len(artifacts) == 1:
        # Preserve existing tags for the common single-artifact release case.
        return _file_hash(artifacts[0]).hex()

    assets = sorted((artifact.name, _file_hash(artifact)) for artifact in artifacts)
    names = [name for name, _ in assets]
    if len(names) != len(set(names)):
        raise ValueError("release artifacts must have unique filenames")

    digest = hashlib.sha256()
    digest.update(b"ducktape-release-assets-v1\0")
    for name, content_hash in assets:
        encoded_name = name.encode()
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(content_hash)
    return digest.hexdigest()


def _file_hash(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", type=Path, nargs="+")
    args = parser.parse_args()
    print(release_content_hash(args.artifacts))


if __name__ == "__main__":
    main()
