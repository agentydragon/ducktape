"""Compute the content identity for a GitHub release's complete asset set."""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Sequence
from pathlib import Path


def release_content_hash(artifacts: Sequence[Path]) -> str:
    """Return a stable digest that changes when any release asset changes."""
    return _assets_hash([(artifact.name, _file_hash(artifact)) for artifact in artifacts])


def release_content_hash_from_digests(assets: Sequence[tuple[str, str]]) -> str:
    """The same identity, computed from each asset's sha256 instead of its bytes.

    A build event stream already reports every output's content digest, so a
    caller that only needs the identity can skip downloading the assets. Must
    stay byte-identical to release_content_hash or every package republishes once.
    """
    return _assets_hash([(name, bytes.fromhex(digest)) for name, digest in assets])


def _assets_hash(assets: Sequence[tuple[str, bytes]]) -> str:
    if not assets:
        raise ValueError("at least one artifact is required")

    if len(assets) == 1:
        # Preserve existing tags for the common single-artifact release case.
        return assets[0][1].hex()

    ordered = sorted(assets)
    names = [name for name, _ in ordered]
    if len(names) != len(set(names)):
        raise ValueError("release artifacts must have unique filenames")

    digest = hashlib.sha256()
    digest.update(b"ducktape-release-assets-v1\0")
    for name, content_hash in ordered:
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
