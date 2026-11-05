"""Hash declared configured engine inputs into a Rust constant."""

import argparse
import base64
import binascii
import hashlib
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True)
class RootFragment:
    name: str
    records: bytes

    def as_json(self) -> dict[str, str]:
        return {"name": self.name, "records_base64": base64.b64encode(self.records).decode("ascii")}

    @classmethod
    def from_json(cls, value: object) -> RootFragment:
        if not isinstance(value, dict):
            raise ValueError("analysis identity root fragment must be an object")
        name = value.get("name")
        encoded_records = value.get("records_base64")
        if not isinstance(name, str):
            raise ValueError("analysis identity root fragment name must be a string")
        if not isinstance(encoded_records, str):
            raise ValueError("analysis identity root fragment records must be base64 text")
        try:
            records = base64.b64decode(encoded_records, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("analysis identity root fragment has invalid base64") from error
        return cls(name=name, records=records)


@dataclass(frozen=True)
class AnalysisManifest:
    config: Any
    files: tuple[str, ...]
    cached_files: tuple[str, ...]

    @classmethod
    def from_json(cls, value: object) -> AnalysisManifest:
        if not isinstance(value, dict):
            raise ValueError("analysis identity manifest must be an object")
        if "config" not in value:
            raise ValueError("analysis identity manifest is missing config")
        files = _sorted_paths(value.get("files"), "manifest files")
        cached_files = _sorted_paths(value.get("cached_files"), "manifest cached_files")
        if not set(cached_files).issubset(files):
            raise ValueError("analysis identity cached roots are not present in files")
        return cls(value["config"], files, cached_files)


def _sorted_paths(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
        raise ValueError(f"analysis identity {description} must be a list of paths")
    paths = cast(list[str], value)
    if paths != sorted(set(paths)):
        raise ValueError(f"analysis identity {description} must be sorted unique paths")
    return tuple(paths)


def _record(value: bytes) -> bytes:
    return len(value).to_bytes(8, "little") + value


def _root_record_values(name: str) -> Iterator[bytes]:
    """Yield one root's old-format values in the exact manifest record order."""
    root = Path(name)
    yield name.encode()
    yield b"directory" if root.is_dir() else b"file"
    paths = sorted(root.rglob("*")) if root.is_dir() else [root]
    for path in paths:
        if path.is_dir():
            if path.is_symlink():
                raise ValueError(f"unsupported directory symlink in analysis identity input: {path}")
            yield str(path).encode()
            yield b"directory"
            continue
        yield str(path).encode()
        content = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                content.update(block)
        yield content.digest()


def build_root_fragments(files: Sequence[str]) -> tuple[RootFragment, ...]:
    """Precompute complete encoded records for sorted roots in a toolchain closure."""
    return tuple(
        RootFragment(name=name, records=b"".join(_record(value) for value in _root_record_values(name)))
        for name in files
    )


def parse_root_fragments(value: object) -> tuple[RootFragment, ...]:
    if not isinstance(value, dict):
        raise ValueError("analysis identity cached fragments must be an object")
    roots = value.get("roots")
    if not isinstance(roots, list):
        raise ValueError("analysis identity cached fragments roots must be a list")
    fragments = tuple(RootFragment.from_json(root) for root in roots)
    names = [fragment.name for fragment in fragments]
    if len(set(names)) != len(names):
        raise ValueError("analysis identity cached fragments contain duplicate roots")
    return fragments


def analysis_digest(manifest: AnalysisManifest, cached_fragments: Sequence[RootFragment]) -> str:
    """Replay cached root records at their original positions and hash remaining roots."""
    if len(cached_fragments) != len(manifest.cached_files):
        raise ValueError("analysis identity cached fragment count does not match cached roots")
    cached_names = tuple(fragment.name for fragment in cached_fragments)
    if cached_names != manifest.cached_files:
        raise ValueError("analysis identity cached fragment order does not match manifest")

    cached_by_name = {fragment.name: fragment.records for fragment in cached_fragments}
    digest = hashlib.sha256()
    config = json.dumps(manifest.config, sort_keys=True, separators=(",", ":")).encode()
    digest.update(_record(config))
    for name in manifest.files:
        if name in cached_by_name:
            digest.update(cached_by_name[name])
        else:
            for value in _root_record_values(name):
                digest.update(_record(value))
    return digest.hexdigest()


def _read_json(path: str) -> object:
    return json.loads(Path(path).read_text())


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    cache = modes.add_parser("cache", help="precompute ordered toolchain root records")
    cache.add_argument("manifest")
    cache.add_argument("output")
    hash_mode = modes.add_parser("hash", help="replay cached roots and hash remaining inputs")
    hash_mode.add_argument("manifest")
    hash_mode.add_argument("cached_fragments")
    hash_mode.add_argument("output")
    args = parser.parse_args(argv)

    if args.mode == "cache":
        value = _read_json(args.manifest)
        if not isinstance(value, dict):
            raise ValueError("analysis identity cached-roots manifest must be an object")
        files = _sorted_paths(value.get("files"), "cached roots")
        fragments = build_root_fragments(files)
        serialized = json.dumps(
            {"roots": [fragment.as_json() for fragment in fragments]}, sort_keys=True, separators=(",", ":")
        )
        Path(args.output).write_text(serialized)
        return

    manifest = AnalysisManifest.from_json(_read_json(args.manifest))
    fragments = parse_root_fragments(_read_json(args.cached_fragments))
    digest = analysis_digest(manifest, fragments)
    Path(args.output).write_text(f'pub const ANALYSIS_ENGINE_SHA256: &str = "{digest}";\n')


if __name__ == "__main__":
    main(sys.argv[1:])
