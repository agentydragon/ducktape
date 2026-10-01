#!/usr/bin/env python3
"""Show, and optionally patch, the -soundhw literals in the emulator's QEMU.

The emulator unconditionally appends a sound card to the QEMU command line
(`-soundhw hda:input=off,output=off`, or `-soundhw virtio-snd-pci` with the
VirtioSndCard feature on) even when audio is disabled in the AVD and
-no-audio/-audio none are passed. On arm64 `-machine type=ranchu`, QEMU's
legacy soundhw_init() resolves its bus with pci_find_primary_bus(), which is
not set, so it aborts with "PCI bus not available for hda" before the guest
starts. There is no `none` card to select instead -- the only two are `hda` and
`virtio-snd-pci`, both PCI.

Patching the launcher's own literal to another 8-character QEMU option that
tolerates the argument turns the unconditional sound card into a no-op. That
alone is not enough to boot: arm64 ranchu turns out to have no PCI bus at all,
so every other `-device *-pci` on the generated command line has to be removed
too, by disabling the advanced features that contribute them
(see advancedFeatures.diff).
"""

import shutil
import sys
from pathlib import Path

B = "/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad/sdk/emulator/qemu/linux-x86_64/qemu-system-aarch64-headless"

data = bytearray(Path(B).read_bytes())


def show() -> None:
    off = -1
    while True:
        off = data.find(b"-soundhw", off + 1)
        if off < 0:
            return
        ctx = bytes(data[max(0, off - 70) : off + 80])
        printable = "".join(chr(c) if 32 <= c < 127 else "." for c in ctx)
        print(f"{off}: {printable}")


if len(sys.argv) > 1 and sys.argv[1] == "patch":
    target = int(sys.argv[2])
    assert data[target : target + 8] == b"-soundhw", data[target : target + 8]
    shutil.copy2(B, B + ".orig")
    # "-D" is QEMU's log-file option: it takes exactly one argument and does
    # nothing else, so it swallows the card spec that follows. Writing it into
    # the same slot (NUL-terminated, rest zeroed) leaves every other string in
    # the pool at its original offset.
    data[target : target + 8] = b"-D\x00\x00\x00\x00\x00\x00"
    Path(B).write_bytes(data)
    print(f"patched offset {target}: -soundhw -> -D (backup at {B}.orig)")
else:
    show()
