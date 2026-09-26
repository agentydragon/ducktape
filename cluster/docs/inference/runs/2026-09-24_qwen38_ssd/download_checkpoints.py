"""Fetch the pinned GGUF files with resumable, rate-limited downloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from util.bazel.runfiles import get_required_path, own_repo_rlocation

MANIFEST = "cluster/docs/inference/runs/2026-09-24_qwen38_ssd/checkpoints.json"
HEADROOM_BYTES = 200 * 1024**3
CHUNK_BYTES = 8 * 1024**2


def _manifest_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe checkpoint path in manifest: {value!r}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as checkpoint:
        while chunk := checkpoint.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path: Path, *, size: int, sha256: str) -> bool:
    return path.is_file() and path.stat().st_size == size and _sha256(path) == sha256


def _curl_config(token: str) -> str:
    if not token or any(ord(char) < 0x20 or ord(char) == 0x7F for char in token):
        raise ValueError("HF_TOKEN must be a non-empty, single-line token")
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    return f'header = "Authorization: Bearer {escaped}"\n'


def _download(url: str, partial: Path, token_config: str) -> None:
    command = [
        "curl",
        "--config",
        "-",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--retry",
        "3",
        "--continue-at",
        "-",
        "--limit-rate",
        "60M",
        "--output",
        str(partial),
        url,
    ]
    child_env = os.environ.copy()
    child_env.pop("HF_TOKEN", None)
    try:
        result = subprocess.run(command, input=token_config, text=True, capture_output=True, check=False, env=child_env)
    except FileNotFoundError:
        raise RuntimeError("curl is unavailable; load the repository Nix devshell") from None
    if result.returncode:
        raise RuntimeError(f"curl failed for {partial.name} with exit status {result.returncode}")


def _inside(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"checkpoint path escapes output directory: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="checkpoint storage directory")
    args = parser.parse_args()

    manifest = json.loads(get_required_path(own_repo_rlocation(MANIFEST)).read_text())
    if not isinstance(manifest, list):
        raise ValueError("checkpoint manifest must be a list")

    output = args.output_dir.expanduser()
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    root = output
    pending: list[tuple[str, str, str, Path, Path, int, str, int]] = []
    required_bytes = 0
    seen: set[Path] = set()

    for repository in manifest:
        repo = repository["repo"]
        revision = repository["revision"]
        directory = _manifest_path(repository["directory"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError(f"invalid Hugging Face repository in manifest: {repo!r}")
        if not re.fullmatch(r"[A-Fa-f0-9]{40}", revision):
            raise ValueError(f"invalid pinned revision for {repo}")

        for item in repository["files"]:
            remote_file = _manifest_path(item["path"])
            relative = directory / remote_file
            target = root.joinpath(*relative.parts)
            _inside(root, target)
            partial = target.with_name(target.name + ".partial")
            _inside(root, partial)
            target.parent.mkdir(parents=True, exist_ok=True)
            _inside(root, target)
            _inside(root, partial)
            if target in seen:
                raise ValueError(f"duplicate checkpoint path in manifest: {relative}")
            seen.add(target)

            size = item["bytes"]
            sha256 = item["sha256"]
            if not isinstance(size, int) or size <= 0 or not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
                raise ValueError(f"invalid size or SHA-256 for {relative}")
            if target.is_symlink() or partial.is_symlink():
                raise ValueError(f"refusing symlink checkpoint path: {relative}")
            if target.exists():
                if not _matches(target, size=size, sha256=sha256):
                    raise ValueError(f"existing checkpoint does not match manifest: {relative}")
                print(f"verified existing {relative}")
                continue

            partial_size = partial.stat().st_size if partial.exists() else 0
            if partial_size > size:
                raise ValueError(f"partial checkpoint exceeds manifest size: {relative}")
            if partial_size == size and not _matches(partial, size=size, sha256=sha256):
                raise ValueError(f"complete partial checkpoint has the wrong SHA-256: {relative}")
            required_bytes += size - partial_size
            pending.append((repo, revision, str(remote_file), target, partial, size, sha256, partial_size))

    free_bytes = shutil.disk_usage(output).free
    needed_bytes = required_bytes + (HEADROOM_BYTES if required_bytes else 0)
    if free_bytes < needed_bytes:
        raise RuntimeError(
            f"insufficient free space under {root}: need {needed_bytes / 1024**3:.1f} GiB "
            f"including 200 GiB headroom; have {free_bytes / 1024**3:.1f} GiB"
        )

    token = os.environ.get("HF_TOKEN")
    if pending and token is None:
        raise RuntimeError("set HF_TOKEN in the environment before downloading checkpoints")
    token_config = _curl_config(token) if pending and token is not None else ""

    for repo, revision, remote_file, target, partial, size, sha256, partial_size in pending:
        if partial_size == size:
            partial.replace(target)
            print(f"verified resumed {target.relative_to(output)}")
            continue
        url = f"https://huggingface.co/{quote(repo, safe='/')}/resolve/{revision}/{quote(remote_file, safe='/')}?download=true"
        print(f"downloading {target.relative_to(output)}")
        _download(url, partial, token_config)
        if not _matches(partial, size=size, sha256=sha256):
            actual_size = partial.stat().st_size if partial.exists() else 0
            raise RuntimeError(
                f"downloaded checkpoint failed size/SHA-256 verification: {target.relative_to(output)} "
                f"(expected {size} bytes, got {actual_size}); partial file was kept"
            )
        partial.replace(target)
        print(f"verified {target.relative_to(output)}")


if __name__ == "__main__":
    main()
