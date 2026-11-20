"""Stage the pinned abliterated Qwen checkpoint on the TabbyAPI host."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

REPOSITORY = "ghost-actual/Qwen3.8-Flash-Next-Abliterated-EXL3-2.50bpw"
REVISION = "135402464a8d351ca6ecb6969f2a6f4061681a24"
DIRECTORY = "qwen38-exl3-250-abliterated"


@dataclass(frozen=True)
class CheckpointFile:
    name: str
    size: int
    git_oid: str
    lfs_sha256: str | None

    def verify(self, path: Path) -> None:
        if file_size(path) != self.size:
            raise ValueError(f"incorrect length for {path}")
        with path.open("rb") as stream:
            if self.lfs_sha256 is not None:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
                expected = self.lfs_sha256
            else:
                # A Git blob digest includes the type and size header.
                digest = hashlib.sha1(f"blob {self.size}\0".encode(), usedforsecurity=False)
                actual = hashlib.file_digest(stream, lambda: digest).hexdigest()
                expected = self.git_oid
        if actual != expected:
            raise ValueError(f"digest mismatch for {path}: expected {expected}, got {actual}")


def checkpoint_files() -> list[CheckpointFile]:
    url = f"https://huggingface.co/api/models/{REPOSITORY}/tree/{REVISION}?limit=100"
    with urlopen(url, timeout=30) as response:
        if 'rel="next"' in response.headers.get("Link", ""):
            raise ValueError("unexpected pagination in checkpoint tree")
        entries = json.load(response)
    files = []
    seen = set()
    for entry in entries:
        name = entry["path"]
        if entry["type"] != "file" or name in ("", ".", "..") or "/" in name or name.endswith(".part") or name in seen:
            raise ValueError(f"unexpected checkpoint entry {name!r}")
        seen.add(name)
        files.append(CheckpointFile(name, entry["size"], entry["oid"], entry.get("lfs", {}).get("oid")))
    if not {"config.json", "model.safetensors.index.json", "ngram_embedding.safetensors"} <= seen:
        raise ValueError("checkpoint tree is missing required files")
    return sorted(files, key=lambda file: file.size, reverse=True)


def file_size(path: Path) -> int:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"expected a regular file at {path}")
    return path.stat().st_size if path.exists() else 0


def remaining_bytes(staging: Path, file: CheckpointFile) -> int:
    target = staging / file.name
    partial = target.with_suffix(target.suffix + ".part")
    size = file_size(target) if target.exists() else file_size(partial)
    if size > file.size:
        raise ValueError(f"checkpoint exceeds expected size at {target}")
    return file.size - size


def download(staging: Path, file: CheckpointFile) -> None:
    target = staging / file.name
    if target.exists():
        file.verify(target)
        print(f"verified existing {file.name}", flush=True)
        return

    partial = target.with_suffix(target.suffix + ".part")
    url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{quote(file.name)}"
    for attempt in range(1, 7):
        offset = file_size(partial)
        if partial.exists() and offset == file.size:
            break
        print(f"downloading {file.name}, attempt {attempt}, starting at {offset:,} bytes", flush=True)
        result = subprocess.run(
            [
                "curl",
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "20",
                "--speed-time",
                "75",
                "--speed-limit",
                "16384",
                "--limit-rate",
                "60M",
                "--continue-at",
                "-",
                "--output",
                str(partial),
                url,
            ],
            check=False,
        )
        if result.returncode == 0 and file_size(partial) == file.size:
            break
        if file_size(partial) > file.size:
            raise ValueError(f"oversize download {partial}")
        time.sleep(3 * attempt)
    file.verify(partial)
    partial.rename(target)
    print(f"verified {file.name} ({file.size:,} bytes)", flush=True)


def stage(root: Path) -> None:
    target = root / DIRECTORY
    staging = root / f".{DIRECTORY}.partial"
    files = checkpoint_files()
    with (root / f".{DIRECTORY}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if target.is_symlink() or staging.is_symlink():
            raise ValueError("checkpoint directories must be local directories")
        if target.exists():
            for file in files:
                file.verify(target / file.name)
        else:
            staging.mkdir(exist_ok=True)
            needed = sum(remaining_bytes(staging, file) for file in files)
            if needed > shutil.disk_usage(root).free:
                raise ValueError(f"insufficient space in {root} for {needed:,} remaining bytes")
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda file: download(staging, file), files))
            if {path.name for path in staging.iterdir()} != {file.name for file in files}:
                raise ValueError(f"unexpected files in {staging}")
            if target.exists():
                raise FileExistsError(target)
            staging.rename(target)
    print(f"verified {len(files)} files, {sum(file.size for file in files):,} bytes, at {target}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, default=Path("/games/llm-models"), help="existing model directory")
    args = parser.parse_args()
    stage(args.model_root)


if __name__ == "__main__":
    main()
