"""Build a small ext4 root disk from the Firecracker guest tar."""

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_BLOCK_SIZE = 4096
_MINIMUM_IMAGE_BYTES = 256 * 1024 * 1024
_IMAGE_UUID = "00000000-0000-0000-0000-000000000001"


def _tree_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def _tool(root: Path, name: str) -> Path:
    candidates = (root / "usr/bin" / name, root / "usr/sbin" / name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"{name} is absent from the e2fsprogs closure")


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(f"Usage: {sys.argv[0]} <rootfs.tar> <e2fsprogs-closure.tar> <output.ext4>")

    archive, closure, output = map(Path, sys.argv[1:])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="firecracker-rootfs-", dir=output.parent) as temporary:
        work = Path(temporary)
        root = work / "root"
        tools = work / "tools"
        root.mkdir()
        tools.mkdir()
        subprocess.run(["tar", "--numeric-owner", "--same-owner", "-xf", archive, "-C", root], check=True)
        # Debian's package closure supplies empty directories here. Replace them after
        # extraction; rmdir refuses to discard unexpected package contents.
        for name, target in {"var/lock": "/run/lock", "var/run": "/run"}.items():
            path = root / name
            path.rmdir()
            path.symlink_to(target)
        subprocess.run(["tar", "-xf", closure, "-C", tools], check=True)

        libdir = tools / "usr/lib/x86_64-linux-gnu"
        loader = libdir / "ld-linux-x86-64.so.2"
        if not loader.is_file():
            raise FileNotFoundError(f"the e2fsprogs closure has no x86_64 loader at {loader}")

        contents_bytes = _tree_bytes(root)
        image_bytes = max(_MINIMUM_IMAGE_BYTES, contents_bytes + max(64 * 1024 * 1024, contents_bytes // 5))
        image_bytes = (image_bytes + _BLOCK_SIZE - 1) // _BLOCK_SIZE * _BLOCK_SIZE
        output.write_bytes(b"")
        with output.open("r+b") as image:
            image.truncate(image_bytes)

        environment = os.environ.copy()
        environment["E2FSPROGS_FAKE_TIME"] = environment.get("SOURCE_DATE_EPOCH", "1")
        mke2fs_config = tools / "etc/mke2fs.conf"
        if not mke2fs_config.is_file():
            raise FileNotFoundError(f"the declared e2fsprogs closure has no mke2fs config at {mke2fs_config}")
        environment["MKE2FS_CONFIG"] = str(mke2fs_config)
        subprocess.run(
            [
                str(loader),
                "--library-path",
                str(libdir),
                str(_tool(tools, "mke2fs")),
                "-F",
                "-q",
                "-m",
                "0",
                "-b",
                str(_BLOCK_SIZE),
                "-L",
                "agentplane-root",
                "-U",
                _IMAGE_UUID,
                "-E",
                f"lazy_itable_init=0,lazy_journal_init=0,root_owner=0:0,hash_seed={_IMAGE_UUID}",
                "-d",
                str(root),
                str(output),
                str(image_bytes // _BLOCK_SIZE),
            ],
            check=True,
            env=environment,
        )
        print(f"Firecracker test rootfs: content={contents_bytes} image={image_bytes} bytes")


if __name__ == "__main__":
    main()
