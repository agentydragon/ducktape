# reverse_engineer skill — open work

## `test_binary_diff_demo` pins toolchain-specific golden values

`examples/binary_diff_recipe.sh` hardcodes exact instruction PCs and instruction
bytes for three functions (`assert_eq` on literal hex). These are artifacts of
the Go compiler's codegen, not of the diffing technique — they broke and needed
regenerating when this repo's `go_sdk` toolchain version bumped (1.26.2 →
1.26.4), and will break again on any future Go/garble version change.

String VMAs and garbled function names proved toolchain-stable across that bump
(`.rodata` layout and garble's seed+symbol-based naming didn't move) — only the
PC/instruction-bytes assertions are fragile.

See if it's worth replacing the exact-byte assertions with structural/invariant
checks instead: verify the found bytes match the `lea reg, [rip+disp32]` opcode
shape (`48 8d 05 XX XX XX XX`, not literal `XX` values) and that decoding the
little-endian displacement plus `PC+7` reproduces the target VMA. That proves
the technique correctly locates and decodes the instruction without pinning to
one compiler's output. Tradeoff: the script's header comment currently doubles
as a worked-example reference with fixed numbers for a human reading along;
decoupling from hardcoded values means those become "whatever this run
computed" rather than a citable snapshot — a fine trade for CI robustness, but
worth deciding deliberately rather than doing by default.

## Undefeated garble techniques in environment-manager

`environment-manager` uses garble with default flags (confirmed by binary analysis):

- identifier/package name obfuscation ✓ defeated (pclntab + pclntool pc)
- pclntab magic XOR ✓ defeated (pclntool patch)
- module info stripped (`go version -m` → unknown)
- symtab stripped (no `.symtab` section)
- DWARF stripped (no debug sections)

**Not in use** in environment-manager (confirmed by string scan): `-literals`, `-tiny`,
`GARBLE_EXPERIMENTAL_CONTROLFLOW`.

### Import path obfuscation

**What garble does**: replaces every package import path with a hash-derived string
(e.g., `main` → `HWp5H9d4l`). Package names in type metadata, panic messages, and
`.gopclntab` are all mangled.

**Current state**: pclntool + redress + GoReSym recover garbled function names from
the patched binary, but the names are still opaque hashes — we can't yet map
`HWp5H9d4l.WpqfRvz` back to `main.connectToServer`.

**Defeat method — binary diffing**:

1. Obtain or build an unobfuscated version of the binary (CI artifact, staging build,
   or community-maintained version with symbols).
2. Import both binaries into Ghidra or use `radiff2 -Cj old new` to match functions
   by call graph structure and string reference sets.
3. Transfer names from the unobfuscated binary to the garbled one.
4. For functions unique to the garbled version (new since the reference build), fall
   back to string-anchored naming: use `pclntool pc` to find the garbled function that
   loads each distinctive string, then assign a descriptive name from the string
   content.

### Function/type name recovery without a reference binary

**What's needed**: when no unobfuscated reference exists, recover human-readable
names from disassembly alone.

**Defeat method**:

1. Use redress/GoReSym on the pclntab-patched binary to list all garbled function
   names and sizes.
2. For each function, run `pclntool pc` on representative PCs, collect string xrefs.
3. Cluster functions by their string content (log messages, error texts, JSON tags)
   to infer purpose.
4. Rename functions manually based on distinctive strings (e.g., a function loading
   `"failed to authenticate"` is an auth handler).

This is manual but systematic — every string is a naming anchor.

## Future: `-literals` and `GARBLE_EXPERIMENTAL_CONTROLFLOW`

Not used in environment-manager. If encountered: `-literals` encrypts string literals
with per-string XOR init functions (defeat: static emulation or Frida hook on
`runtime.convTstring`). `GARBLE_EXPERIMENTAL_CONTROLFLOW` inserts junk blocks into
the CFG (defeat: Ghidra's `DecompilerSwitchAnalyzer`).
