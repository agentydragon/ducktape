#!/usr/bin/env python3
"""Carve the ext4 partition out of an emulator system/vendor image.

These are GPT disk images, not bare filesystems, and this host kernel has no
loop or ext4 support, so the partition is dd'd out by offset and then read
with debugfs.
"""

import struct
import subprocess
import sys
from pathlib import Path

img, out = sys.argv[1], sys.argv[2]
with Path(img).open("rb") as f:
    f.seek(512)
    hdr = f.read(92)
    assert hdr[:8] == b"EFI PART", hdr[:8]
    part_lba, nparts, psize = (
        struct.unpack("<Q", hdr[72:80])[0],
        struct.unpack("<I", hdr[80:84])[0],
        struct.unpack("<I", hdr[84:88])[0],
    )
    f.seek(part_lba * 512)
    for i in range(nparts):
        e = f.read(psize)
        if e[:16] == b"\x00" * 16:
            continue
        first, last = struct.unpack("<QQ", e[32:48])
        name = e[56:128].decode("utf-16-le").rstrip("\x00")
        print(f"part {i}: name={name!r} lba {first}..{last} ({(last - first + 1) * 512 / 1e6:.1f} MB)")
        if i == 0:
            subprocess.run(
                ["dd", f"if={img}", f"of={out}", "bs=512", f"skip={first}", f"count={last - first + 1}", "status=none"],
                check=True,
            )
            print(f"wrote {out}")
