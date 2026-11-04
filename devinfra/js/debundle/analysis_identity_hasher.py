"""Temporary grouped profiles of the unchanged identity byte stream."""

import cProfile
import hashlib
import json
import sys
from pathlib import Path


def hash_files(names, record, inventory):
    for name in names:
        root = Path(name)
        record(name.encode())
        record(b"directory" if root.is_dir() else b"file")
        paths = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in paths:
            if path.is_dir():
                if path.is_symlink():
                    raise ValueError(f"unsupported directory symlink in analysis identity input: {path}")
                record(str(path).encode())
                record(b"directory")
                continue
            record(str(path).encode())
            content = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    content.update(block)
                    size += len(block)
            record(content.digest())
            inventory.append({"path": str(path), "bytes": size})


def main() -> None:
    manifest = json.loads(Path(sys.argv[1]).read_text())
    output = Path(sys.argv[2])
    digest = hashlib.sha256()

    def record(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "little"))
        digest.update(value)

    def external(name):
        return name.startswith("external/") or "/external/" in name

    first_party = [name for name in manifest["files"] if not external(name)]
    dependencies = [name for name in manifest["files"] if external(name)]
    assert first_party + dependencies == manifest["files"], "profile partition must retain exact hash order"
    record(json.dumps(manifest["config"], sort_keys=True, separators=(",", ":")).encode())
    inventories = {}
    for group, names in [("first_party", first_party), ("external", dependencies)]:
        inventory = []
        profiler = cProfile.Profile()
        profiler.runcall(hash_files, names, record, inventory)
        profiler.dump_stats(str(output.with_suffix(f".{group}.cprofile")))
        inventories[group] = inventory
    output.with_suffix(".inventory.json").write_text(json.dumps(inventories))
    output.write_text(f'pub const ANALYSIS_ENGINE_SHA256: &str = "{digest.hexdigest()}";\n')


if __name__ == "__main__":
    main()
