"""Run one erofs-utils program (mkfs.erofs, fsck.erofs, dump.erofs) from a Debian package closure.

Usage: erofs_tool.py <closure.tar> <program> [args...]

The closure carries its own libc, and the RBE image's Ubuntu lacks some of erofs-utils' libraries,
so the program runs on the closure's loader against the closure's libraries.
"""

import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(f"Usage: {sys.argv[0]} <closure.tar> <program> [args...]")
    closure, program, *args = sys.argv[1:]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["tar", "-xf", closure, "-C", tmp], check=True)
        libdir = root / "usr/lib/x86_64-linux-gnu"
        subprocess.run(
            [libdir / "ld-linux-x86-64.so.2", "--library-path", libdir, root / "usr/bin" / program, *args], check=True
        )


if __name__ == "__main__":
    main()
