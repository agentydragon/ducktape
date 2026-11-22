"""Compute the content identity for a GitHub release's complete asset set."""

from __future__ import annotations

import argparse
import hashlib
import re
from collections.abc import Sequence
from pathlib import Path

# Current releases use 12 hex digits. Older releases used seven.
TAG_HASH_LENGTH = 12


def content_tag(pkg: str, content_hash: str) -> str:
    return f"{pkg}-{content_hash[:TAG_HASH_LENGTH]}"


def tag_package(tag: str) -> str | None:
    """Read a current or historical release tag, including hyphenated package names."""
    pkg, _, suffix = tag.rpartition("-")
    if pkg and len(suffix) in (7, TAG_HASH_LENGTH) and re.fullmatch("[0-9a-f]+", suffix):
        return pkg
    return None


def release_content_hash(artifacts: Sequence[Path]) -> str:
    """Return a stable digest that changes when any release asset changes."""
    assets = []
    for artifact in artifacts:
        with artifact.open("rb") as stream:
            assets.append((artifact.name, hashlib.file_digest(stream, "sha256").digest()))
    return _assets_hash(assets)


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("artifacts", type=Path, nargs="+")
    args = parser.parse_args(argv)
    print(content_tag(args.package, release_content_hash(args.artifacts)))


if __name__ == "__main__":
    main()
