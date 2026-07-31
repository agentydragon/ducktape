#!/usr/bin/env python3
"""Recover inline-compared string constants from an obfuscated Go binary.

garble -literals encrypts string *data*, but a comparison against a short
constant compiles that constant into an **instruction operand**, where hiding
it would mean changing the code. Inline-compared strings therefore survive
-literals untouched -- and they are exactly the interesting ones: enum values,
protocol verbs, mode names, error categories.

Go emits such a comparison as a length check plus compares at increasing
displacements off the string's data pointer, so fragments must be reassembled
by displacement rather than by program order:

    cmpq    $0xd, 0x28(%rcx)              ; len(s) == 13
    movabsq $0x632d656d75736572, %r12     ; "resume-c"
    cmpq    %r12, (%rdx)
    cmpl    $0x65686361, 0x8(%rdx)        ; "ache"
    cmpb    $0x64, 0xc(%rdx)              ; 'd'      -> "resume-cached"

The guarding length check is a free correctness signal: if a reassembled
string's length disagrees with the `cmpq $N` next to it, the reassembly is
wrong.

Usage:

    inline_strings <binary> <funcs.txt>

where funcs.txt holds "0xADDR name" per line -- e.g. from gosymtab piped
through `go tool nm`. Pass /dev/null to attribute everything to "?".
"""

import bisect
import re
import subprocess
import sys
from pathlib import Path

INSN = re.compile(r"^\s*([0-9a-f]+):\s+[0-9a-f ]+\t(\S+)\t?([^#\n]*)")
IMM = re.compile(r"\$(-?0x[0-9a-f]+)")
MEM = re.compile(r"(-?0x[0-9a-f]+)?\(%(\w+)[,)]")
WIDTH = {"cmpq": 8, "movq": 8, "cmpl": 4, "movl": 4, "cmpw": 2, "movw": 2, "cmpb": 1, "movb": 1}


def ascii_ok(b):
    return all(0x20 <= c < 0x7F for c in b)


def load_funcs(path):
    addrs, names = [], []
    with Path(path).open() as fh:
        for line in fh:
            parts = line.split(None, 1)
            if len(parts) == 2:
                addrs.append(int(parts[0], 16))
                names.append(parts[1].strip())
    order = sorted(range(len(addrs)), key=lambda i: addrs[i])
    return [addrs[i] for i in order], [names[i] for i in order]


def fragment(mnemonic, ops, pending):
    """Return (displacement, bytes, base_register) if this instruction compares
    against an ASCII constant, else None.
    """
    imm, mem = IMM.search(ops), MEM.search(ops)
    if mnemonic.startswith("cmp") and mem and not imm:
        # cmpq %reg, disp(%base): the constant came from an earlier movabs.
        b = pending.get(ops.split(",")[0].strip().lstrip("%"))
        if b:
            return (int(mem.group(1), 16) if mem.group(1) else 0, b, mem.group(2))
        return None
    if mnemonic in WIDTH and imm and mem:
        width = WIDTH[mnemonic]
        value = int(imm.group(1), 16) & ((1 << (width * 8)) - 1)
        b = value.to_bytes(width, "little")
        if ascii_ok(b):
            return (int(mem.group(1), 16) if mem.group(1) else 0, b, mem.group(2))
    return None


def main():
    if len(sys.argv) != 3:
        print("usage: inline_strings <binary> <funcs.txt>", file=sys.stderr)
        return 2
    binary, funcs = sys.argv[1], sys.argv[2]
    addrs, names = load_funcs(funcs)

    def func_at(pc):
        i = bisect.bisect_right(addrs, pc) - 1
        return names[i] if i >= 0 else "?"

    pending = {}  # register -> ASCII bytes loaded by a movabs
    run = None  # (func, base, next_disp, buf, start_pc)
    seen = set()

    def emit(state):
        if not state:
            return
        func, _, _, buf, pc = state
        s = bytes(buf).decode()
        if len(s) >= 4 and re.search(r"[A-Za-z]{3}", s) and (func, s) not in seen:
            seen.add((func, s))
            print(f"{func}\t{hex(pc)}\t{s}")

    proc = subprocess.Popen(
        ["objdump", "-d", "-j", ".text", binary], stdout=subprocess.PIPE, text=True, bufsize=1 << 20
    )
    for line in proc.stdout:
        m = INSN.match(line)
        if not m:
            continue
        pc, mnemonic, ops = int(m.group(1), 16), m.group(2), m.group(3).strip()

        if mnemonic.startswith("movabs"):
            imm, mem = IMM.search(ops), MEM.search(ops)
            if imm and not mem:
                value = int(imm.group(1), 16) & ((1 << 64) - 1)
                b = value.to_bytes(8, "little")
                pending[ops.split(",")[-1].strip().lstrip("%")] = b if ascii_ok(b) else None
                continue

        frag = fragment(mnemonic, ops, pending)
        if frag is None:
            continue
        disp, b, base = frag
        func = func_at(pc)
        if run and run[0] == func and run[1] == base and run[2] == disp:
            run = (func, base, disp + len(b), run[3] + b, run[4])
        else:
            emit(run)
            run = (func, base, len(b), bytearray(b), pc) if disp == 0 else None
    emit(run)
    proc.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
