"""Package a sparse, empty ext4 filesystem for the Docker recycling experiment."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    closure, output = map(Path, sys.argv[1:])
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        tools = work / "tools"
        tools.mkdir()
        subprocess.run(["tar", "-xf", closure, "-C", tools], check=True)
        image = work / "k3s-recycled.ext4"
        with image.open("xb") as backing:
            backing.truncate(32 * 1024**3)
        libdir = tools / "usr/lib/x86_64-linux-gnu"
        subprocess.run(
            [
                libdir / "ld-linux-x86-64.so.2",
                "--library-path",
                libdir,
                tools / "usr/sbin/mke2fs",
                "-t",
                "ext4",
                "-F",
                "-q",
                "-m",
                "0",
                "-E",
                "lazy_itable_init=0,lazy_journal_init=0",
                image,
            ],
            check=True,
            env=os.environ | {"MKE2FS_CONFIG": str(tools / "etc/mke2fs.conf")},
        )
        # Upload allocated extents, not 32 GiB of holes. GNU tar restores the sparse file.
        subprocess.run(["tar", "--sparse", "-czf", output, "-C", work, image.name], check=True)


if __name__ == "__main__":
    main()
