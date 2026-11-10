"""Compile the guest's udev hardware database from its Debian package closure.

The closure is extracted without package scripts, so it ships hwdb sources but no `hwdb.bin`;
without this prebuilt file every fresh root overlay compiles it at boot, and `systemd-udevd`
waits for that before discovering Runner's disks.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    closure, output = map(Path, sys.argv[1:])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["tar", "-xf", closure, "-C", root], check=True)
        libdir = root / "usr/lib/x86_64-linux-gnu"
        # Use the guest's loader and libraries, including systemd's private library, on the builder.
        subprocess.run(
            [
                libdir / "ld-linux-x86-64.so.2",
                "--library-path",
                f"{libdir}:{libdir / 'systemd'}",
                root / "usr/bin/systemd-hwdb",
                f"--root={root}",
                "--usr",
                "--strict",
                "update",
            ],
            check=True,
        )
        shutil.copyfile(root / "usr/lib/udev/hwdb.bin", output)


if __name__ == "__main__":
    main()
