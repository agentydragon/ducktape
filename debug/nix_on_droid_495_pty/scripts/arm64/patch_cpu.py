#!/usr/bin/env python3
"""Show, and optionally patch, the arm64 CPU model the emulator hardcodes.

The emulator always passes `-cpu cortex-a57` for arm64 ranchu, and `-qemu -cpu
<other>` does not override it. Cortex-A57 is ARMv8.0. Google only ever runs
these arm64 images on Apple Silicon under HVF, where `-cpu` is ignored and the
guest sees a real ARMv8.5+ core, so nothing upstream exercises Android 14
userspace on an ARMv8.0 CPU -- and here its init takes an EL0 data abort and
SIGSEGVs about a second after starting.

Rewriting the literal in place (NUL-terminated, rest zeroed) leaves every other
string in the pool at its original offset.
"""

import shutil
import sys
from pathlib import Path

B = "/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad/sdk/emulator/qemu/linux-x86_64/qemu-system-aarch64-headless"

data = bytearray(Path(B).read_bytes())

if len(sys.argv) > 2 and sys.argv[1] == "patch":
    target, model = int(sys.argv[2]), sys.argv[3].encode()
    assert data[target : target + 10] == b"cortex-a57", data[target : target + 10]
    assert len(model) <= 10, model
    if not Path(B + ".cpu-orig").exists():
        shutil.copy2(B, B + ".cpu-orig")
    data[target : target + 10] = model + b"\x00" * (10 - len(model))
    Path(B).write_bytes(data)
    print(f"patched offset {target}: cortex-a57 -> {model.decode()}")
else:
    off = -1
    while True:
        off = data.find(b"cortex-a57", off + 1)
        if off < 0:
            break
        ctx = bytes(data[max(0, off - 90) : off + 60])
        print(f"{off}: " + "".join(chr(c) if 32 <= c < 127 else "." for c in ctx))
