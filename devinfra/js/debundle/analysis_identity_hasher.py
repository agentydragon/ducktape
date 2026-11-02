"""Hash declared configured engine inputs into a Rust constant."""

import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    manifest = json.loads(Path(sys.argv[1]).read_text())
    digest = hashlib.sha256()

    def record(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "little"))
        digest.update(value)

    record(json.dumps(manifest["config"], sort_keys=True, separators=(",", ":")).encode())
    for name in manifest["files"]:
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
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    content.update(block)
            record(content.digest())
    Path(sys.argv[2]).write_text(f'pub const ANALYSIS_ENGINE_SHA256: &str = "{digest.hexdigest()}";\n')


if __name__ == "__main__":
    main()
