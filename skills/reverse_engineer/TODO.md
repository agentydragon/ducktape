# reverse_engineer skill — open work

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

**Recipe** (CI-verified): `examples/binary_diff_recipe.sh` + `examples/test_scaffold_binary_diff.sh`.

- Builds victim_v1 plain (`go build` from `cmd/garble_target`).
- Builds victim_v2 garbled (`garble build` from `cmd/garble_target_v2`, which adds `validateConfig`).
- Asserts: shared strings ("connection refused", "failed to read config file") anchor to the
  correct named v1 functions and to distinct garbled v2 functions.
- Asserts: v2-only string ("invalid port: must be between 1 and 65535") is absent from v1 and
  maps to a distinct garbled function in v2 (the new `validateConfig`).

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
