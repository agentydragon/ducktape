---
name: reverse_engineer
description: Systematic binary reverse engineering toolkit. Extract source code, understand functions, document protocols, compare versions. Uses strings, symbols, disassembly, and differential verification.
argument-hint: "<path/to/binary> [language]"
allowed-tools: Bash, Read, Grep, Glob, Edit, Write, Task, WebFetch
---

# Reverse Engineering Toolkit

**Argument:** `$ARGUMENTS` — path to binary (or `.gz`-compressed), optionally target language.

**The binary is ground truth.** Every decision traces to binary evidence — strings, disassembly, syscalls, or runtime behavior. Your opinions about how code "should" look are irrelevant when they contradict the binary.

**Every binary is reversible.** Stripping removes metadata, not behavior. Obfuscation renames symbols, not logic. The CPU executes the same instructions regardless of whether you have debug info. You always have: the full instruction stream, string literals, the syscall interface, and runtime behavior. Adjust techniques, not ambition.

**When a tool fails, diagnose and build.** A tool returning an error or empty output is not a dead end — it is a precise description of what the binary violates. Read the error, find the exact check that failed, determine what the binary does instead, and write the minimal fix. Add the fix to `examples/` and record it in the Defeating Obfuscation section below. This skill is a living document: every obfuscation technique you defeat belongs here so the next run starts ahead.

---

## Classify First

```bash
BINARY="$1"
file "$BINARY"
readelf -n "$BINARY" | grep "Build ID"

# Stripped?
nm "$BINARY" 2>/dev/null | grep -q ' T ' && echo "SYMBOLS" || echo "STRIPPED"

# DWARF?
readelf --debug-dump=info "$BINARY" 2>/dev/null | grep -q DW_TAG && echo "DWARF" || echo "NO DWARF"

# Go garble? (returns "unknown" when garbled)
go version -m "$BINARY" 2>&1 | grep -q unknown && echo "GARBLE-OBFUSCATED"

# Go: enumerate functions from pclntab (non-garbled only — garble XORs the magic)
# Install: go install github.com/goretk/redress@latest
redress packages "$BINARY"      # packages (returns empty on garbled binaries)
# For garbled binaries, use pclntool (see examples/pclntool.go)

# Application string count
strings "$BINARY" | grep -cE '(error|fail|flag|usage|http|/v[0-9])'
```

| Class          | Symbols | DWARF | Approach                                          |
| -------------- | ------- | ----- | ------------------------------------------------- |
| **Unstripped** | Yes     | Yes   | DWARF + symbol extraction → source reconstruction |
| **Stripped**   | No      | No    | String-anchored Ghidra/objdump decompilation      |
| **Obfuscated** | Mangled | No    | Disassembly + runtime probing + string analysis   |

---

## Workflow

### 1. Census (mandatory before writing any source)

Produce a written inventory:

- **Binary metadata**: arch, language, compiler, linking, stripped/obfuscated?
- **String checklist**: every application-level string, each marked UNCOVERED.
  Updated as code is written. Missing strings = missing logic.
- **Dependencies**: crates/packages/libraries with versions (from strings,
  `.comment`, embedded module paths)
- **Module structure**: from source path strings (e.g., `/build/src/io.rs`
  in Rust panics, Go package paths in type metadata)
- **Function inventory**: known functions with sizes, grouped by module

Key extractions:

- `strings -n 6` — log messages, error paths, CLI flags, URLs, JSON fields
- `readelf -S` — section sizes (`.text`, `.rodata`, `.data`)
- `nm` / `nm -D` — symbols (if available)
- Source path strings — Rust panics embed `/build/src/*.rs`, Go embeds
  package paths

### 2. Project skeleton

- Build config matching the binary (Cargo.toml/BUILD.bazel for Rust,
  go.mod for Go) with dependency versions from embedded strings
- Module files matching discovered source paths
- String coverage tracking (checklist mapping each string to source location)

### 3. Function-by-function reconstruction

For each function:

1. **Anchor on strings** — every string reference dictates error paths, log
   messages, CLI flags, file paths
2. **Read the disassembly** — `objdump -d`, `go tool objdump`, or Ghidra
   decompilation. Follow control flow: branches, loops, calls, resource
   lifecycle
3. **Write source** with doc comment citing binary evidence (offset, string
   refs), exact string literals from the binary, matching control flow
4. **Mark coverage** — update string checklist. Unplaced strings = missing code
5. **Mark gaps** with `TODO(re): <description>` (see below)

### 4. Differential verification (mandatory)

```bash
# String diff — every missing string is a reconstruction bug
comm -23 <(strings -n 6 "$REF" | sort -u) <(strings -n 6 "$YOURS" | sort -u) > /tmp/missing.txt

# Symbol diff (if applicable)
diff <(nm -D "$REF" | sort) <(nm -D "$YOURS" | sort)

# Compile your RE source and compare against the target binary
# String coverage: every line in missing.txt is a code path not yet reconstructed
go build -o /tmp/re-binary ./...
comm -23 \
  <(strings -n 6 "$REF" | sort -u) \
  <(strings -n 6 /tmp/re-binary | sort -u) \
  > /tmp/missing_strings.txt

# Function-level structural comparison with radiff2 (radare2)
# Shows function similarity scores — unmatched functions = structural gaps
radiff2 -s "$REF" /tmp/re-binary | sort -k3 -n | head -40

# Behavioral diff
strace -f "$REF" <args> 2>/tmp/ref.strace &
strace -f "$YOURS" <args> 2>/tmp/my.strace &

# Stub scan
grep -rn 'TODO(re)' src/ | tee /tmp/todo_re.txt
```

---

## Stripped Binaries

No symbols — use strings as your primary anchor and disassembly for everything else.

- **Function discovery**: every string literal has a `.rodata` address. Code
  referencing that address is the function using that string. For Rust, panic
  messages embed source paths (`/build/src/*.rs`) revealing module structure.
- **Ghidra headless**: primary tool. Recovers function boundaries and produces
  C pseudocode from raw instructions. Use `analyzeHeadless` for batch processing.
- **Without Ghidra**: `objdump -d` + manual analysis. Identify functions by
  prologue patterns (`push %rbp; mov %rsp,%rbp`), call targets (`callq 0xADDR`),
  and alignment boundaries.
- **Serde/serialization fields**: stripped Rust binaries still contain field
  name strings from derive macros. `strings | grep 'struct.*with.*elements'`
  reveals struct definitions.
- **Translation**: Ghidra produces C pseudocode. Pattern-match Rust
  `Result<T,E>` (tag+union), `Vec<T>` (ptr+len+cap), `String` (same),
  trait object vtables. For Go: slice headers, interface values, goroutine
  spawn patterns.

---

## Obfuscated Binaries

Obfuscation (garble, UPX, symbol mangling) makes static analysis harder but
not impossible. The instruction stream is still there — read it.

**What's preserved**: runtime behavior (CLI, network, files), serialization
formats (JSON tags, protobuf field names), embedded content (templates,
scripts), syscall patterns, `runtime.pclntab` (function boundaries), and the
full disassembly. String literals are preserved unless garble's `-literals`
flag was used (see Garble Literal Deobfuscation below).

**What's destroyed**: symbol names, DWARF, `go version -m` output, package
paths in type metadata, and (with `-literals`) most string constants.

**Techniques:**

- **Read the assembly directly.** `objdump -d` works on every binary.
  Obfuscation randomizes names but the instructions are identical. Follow
  call chains from known entry points (e.g., `_start` → `main`). Cross-reference
  string loads to identify what each function does.
- **Runtime probing**: `--help`, `--version`, strace for syscalls, ltrace for
  library calls. CLI help text is always preserved.
- **String analysis**: garble doesn't touch string literals (unless `-literals`
  is used — see below). Extract all strings — log messages, error texts, CLI
  flags, API endpoints, JSON tags, gRPC service names, embedded scripts.
  These are your anchors.
- **Binary diffing** (see below): when a prior unobfuscated version exists,
  use BinDiff/Diaphora to automatically match functions across versions and
  recover original names. This is the highest-leverage technique.
- **Behavioral validation**: run both binaries with identical inputs, compare
  outputs, syscall traces, network traffic. Externally observable behavior
  is ground truth regardless of obfuscation.

### Defeating Obfuscation: Build What's Missing

When a tool fails, follow this pattern:

1. **Read the error exactly.** `"no symbol section"`, `"failed to locate pclntab"`,
   `"unknown"` — each error names the specific check that failed.
2. **Find what the binary has instead.** `readelf -S` for sections, hex dump
   the relevant bytes, compare to what the tool expects.
3. **Write the minimal fix** — a small Go/Python program, a patch, or a shell
   function that repairs or works around the specific difference.
4. **Verify it works** against the actual binary, then add it to `examples/`
   with a comment explaining the obfuscation it defeats.
5. **Record it in the table below** and update this skill so future runs
   start with the fix already available.

**Known-defeated techniques** (tools in `examples/`):

| Technique                                       | What breaks                                                                      | Fix                                                                                  | File                   |
| ----------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ---------------------- |
| garble XORs `.gopclntab` magic bytes (v0.13.0+) | `go tool objdump`, `redress`, `GoReSym`, `debug/gosym` all fail to parse pclntab | `pclntool patch` writes a repaired binary; `pclntool pc` maps a PC to its garbled fn | `examples/pclntool.go` |
| garble strips ELF `.symtab`                     | `go tool objdump` fails with "no symbol section"                                 | Use GNU `objdump -d` instead; it does not require `.symtab`                          | (no file needed)       |
| garble strips module info from `.go.buildinfo`  | `go version -m` returns `unknown`                                                | Use Go compiler version from `redress info` or `readelf` on `.go.buildinfo` section  | (no file needed)       |

When you encounter a new technique not in this table, defeat it and add a row.

### Go-Specific: Function Enumeration from pclntab

Garble randomizes symbol names but **cannot remove `runtime.pclntab`** — the
PC-to-line-number table the Go runtime needs for stack traces. On
**non-garbled** stripped Go binaries, `redress` parses pclntab to list
function boundaries:

```bash
go install github.com/goretk/redress@latest
redress packages ./binary      # package list with function counts
redress source ./binary        # source-level view
```

**On garble-obfuscated binaries `redress` returns empty output.** Garble v0.13.0+
XORs the first 4 bytes of `.gopclntab` (the magic number) with a seed-derived
key. `redress`, `GoReSym`, and `debug/gosym` all fail — they expect a known
magic and get garbage. This is a defeated technique: `pclntool`
(`examples/pclntool.go`) patches each known Go magic in-memory until
`gosym.NewTable` succeeds. See `examples/garble_re_recipe.sh` for the full
workflow: pclntab deobfuscation → redress/GoReSym enumeration → string →
byte offset → VMA → instruction PC → function name.

### Go-Specific: Instruction-Level Analysis and String-to-Function Anchoring

`go tool objdump` requires an ELF symbol table and fails on garbled binaries
(`no symbol section`). `go tool addr2line` similarly fails on garbled binaries
(returns `?` for all addresses). Use `objdump -d` (GNU binutils) for disassembly.
For pclntab-based PC→function mapping, use `pclntool` (see `examples/pclntool.go`):
garble v0.13.0+ obfuscates the `.gopclntab` magic bytes, so standard Go tooling
that reads pclntab won't work until the magic is repaired.

**Workflow:** read `examples/garble_re_recipe.sh` — it is a runnable, CI-verified
demonstration with inline commentary explaining each step.

```bash
# Step 6: correlate register/offset patterns to struct fields
# LEA rdi, [rcx+0x30]  →  receiver field at offset 0x30
# MOVQ [rsp+0x18], rax →  stack argument at position 3
# Cross-reference with RTTI struct layouts to name the fields
```

**Reading call arguments from disassembly:**

Go's register ABI (since 1.17) passes the first ~9 integer arguments in
`AX, BX, CX, DI, SI, R8, R9, R10, R11`. Before a `CALL` instruction, read
which registers are loaded:

```
MOVQ 0x30(CX), AX    ; AX = receiver.fieldAtOffset0x30
LEAQ 0x10(SP), BX    ; BX = pointer to stack local at +0x10
CALL some_func
```

Compare offsets to struct RTTI (manual `readelf` on type metadata sections)
to name the arguments. This directly resolves "gitProxyConfig source —
garble-obfuscated" style TODOs.

### Go-Specific: Garble Literal Deobfuscation

When garble is built with `-literals`, string constants are encrypted at
compile time and decrypted by generated `init()` functions using XOR/shift
sequences. This affects constants like env var names that appear nowhere in
`strings` output.

**Detection:**

```bash
# If a known constant is missing from strings output, it may be encrypted
strings "$BINARY" | grep -c "KNOWN_STRING"   # → 0 means likely encrypted

# Look for byte-array init patterns in disassembly
# (use GNU objdump -d, not go tool objdump — the latter fails on garbled binaries)
objdump -d "$BINARY" | grep -E 'movb|xorb' | head -20
```

**Recovery via strace:**

The binary decrypts strings at runtime. In principle, strace can observe the
results as arguments to write/sendto or in environment lookups. This has not
been verified against an actual `-literals` binary — treat as a starting point:

```bash
strace -e trace=write -s 512 ./binary --help 2>&1
strace -e trace=openat,read -s 256 ./binary --help 2>&1
```

**Recovery via disassembly:**

Each encrypted string has a generated `init` function that decrypts it in
place using XOR/shift sequences. The general shape to look for (not verified
against a real `-literals` binary — treat as illustrative):

```asm
; Encrypted bytes loaded into a stack buffer, then XOR'd in place
MOVB $0x43, 0x00(SP)
MOVB $0x57, 0x01(SP)
XORB $0x14, 0x00(SP)
XORB $0x14, 0x01(SP)
; the buffer now holds the decrypted string
```

Emulate the sequence manually or with a short Python script to recover the
plaintext.

**Go-specific: garble preserves struct tags.** Even with `-literals`, garble
cannot encrypt `json:"field_name"` struct tags because the `encoding/json`
package reads them via reflection at runtime. Extract all tags:

```bash
strings "$BINARY" | grep -oP 'json:"[^"]*"' | sort -u
strings "$BINARY" | grep -oP '[a-z_]+,omitempty' | sort -u
```

These reveal the complete wire format of every serialized struct.

### Binary Diffing: Recovering Names from a Prior Version

When you have two versions of the same binary — one with symbols (old) and one
obfuscated (new) — binary diffing tools can automatically match functions across
versions and transfer names from old→new. This is dramatically more efficient
than manual RE of the obfuscated binary.

**Tools (in order of preference):**

1. **BinDiff** (Google, free) — Ghidra or IDA plugin. Matches functions by
   call graph structure, string references, basic block count, instruction
   mnemonics. Not tested in this skill — documented based on tool documentation.
2. **Diaphora** (open source) — Ghidra/IDA plugin. Similar matching but also
   compares pseudo-code ASTs. Not tested in this skill.
3. **radiff2** (radare2) — CLI-based structural diffing. Tested for the
   compile-verify loop (garbled target vs. freshly compiled RE binary). Not
   tested for garbled-vs-garbled comparison across versions.
4. **Custom string-anchored matcher** — when tools aren't available, a script
   can match functions by their string reference sets (see below).

**BinDiff/Diaphora** (reported workflow, not verified in this skill):

```bash
# Import both into Ghidra projects, run the plugin, get a function mapping:
# pairs of (old_name, old_addr) → (new_addr, similarity_score)
analyzeHeadless /tmp/ghidra_old OldProject -import "$OLD_BINARY"
analyzeHeadless /tmp/ghidra_new NewProject -import "$NEW_BINARY"
# Then run BinDiff or Diaphora from the Ghidra GUI or headless script
```

**Compile-verify loop with `radiff2`:**

After writing RE source for the current binary, compile it and compare against
the target. This closes the verification loop without requiring Ghidra.

```bash
# Build your RE source
go build -o /tmp/re-binary ./...

# String coverage gap — every line is a code path not yet reconstructed
comm -23 \
  <(strings -n 6 "$TARGET" | sort -u) \
  <(strings -n 6 /tmp/re-binary | sort -u) \
  > /tmp/missing_strings.txt
wc -l /tmp/missing_strings.txt   # shrink this to zero

# Function-level structural diff
# Columns: addr_in_target | addr_in_yours | similarity_score | name
radiff2 -s "$TARGET" /tmp/re-binary 2>/dev/null | sort -k3 -n
# similarity 1.0 → identical; < 0.8 → logic diverges; missing → new/removed function

# Count unmatched functions (in target but not in your binary)
radiff2 -s "$TARGET" /tmp/re-binary 2>/dev/null | awk '$3 == "UNMATCH" {count++} END {print count, "unmatched"}'
```

The string diff tells you _what_ is missing; `radiff2` tells you _which function_
is missing or has different logic. Use them together: find a missing string,
trace it to the garbled function via `pclntool` + `objdump -d`, write the
function, rebuild, and watch both metrics improve.

**Version-to-version function delta with `radiff2`:**

`radiff2` is tested for the compile-verify loop (garbled target vs. freshly
compiled RE binary with symbols). Whether it matches functions usefully between
two garbled binaries — where both have randomized names and potentially
different code layout — is untested. It may work via instruction-level
structural matching, but results should be treated with skepticism until
verified empirically.

**String-anchored matching (no tools required):**

When BinDiff/Diaphora aren't available, you can match functions by their
string references alone. Most application functions reference at least one
unique string (log message, error text, format string). Functions referencing
the same set of strings are the same function.

```bash
# 1. For old binary (has symbols): map function → strings it references
#    nm "$OLD" gives symbol names and addresses; objdump -d gives disassembly
#    to find which strings each function loads

# 2. For new garbled binary: find all string-referencing code sites
#    strings -t x "$NEW" gives (file-offset, string) pairs
#    Convert offset to VMA via readelf -S, then use pclntool to map PC → garbled fn

# 3. Match: old function refs {"error A", "log B"} = new function refs same strings
#    → new garbled function = old named function

# 4. Identify NEW functions: string refs in new binary with no match in old
#    → these are new or changed code, need manual RE
```

**What to do with the mapping:**

- Update all address annotations in RE source: `old_addr → new_addr`
- For matched functions with high confidence: mark as `VERIFIED`
- For unmatched functions in the new binary: these are new code — prioritize
  for manual RE
- For functions in old but not new: removed code — delete from RE
- For functions with partial matches (same strings but different size):
  changed logic — compare disassembly and update RE

---

## Marking Incomplete Work

Use exactly `// TODO(re):` (or `# TODO(re):`) prefix for all reconstruction
gaps. Every marker must include a description of what should be there.

Required markers:

- Stub function bodies: `// TODO(re): stub — <purpose>`
- Placeholder types (`interface{}`, `any`): `// TODO(re): concrete type not recovered`
- Discarded values (`_ = expr`): `// TODO(re): should be <how value is consumed>`
- Unrecovered closures/goroutines: `// TODO(re): not reconstructed — <description>`
- Missing error handling: `// TODO(re): error handling not reconstructed`

**Unmarked stubs are bugs.** A function returning nil where the binary has
real logic, an empty closure body, or a discarded value — all must be marked.
The marker makes the gap visible and greppable.

**No dead code.** Optimized binaries don't contain dead code. Everything is
reachable. If nothing calls your code, your call graph is wrong.

**No `#[allow(dead_code)]` / `// nolint` suppression.** These mean you failed
to find the caller.

---

## Confidence Levels

When documenting RE provenance, mark each module/function:

- `VERIFIED` — behavior confirmed against the target binary (runtime test,
  string match, or disassembly match)
- `CARRIED` — copied from a prior version's RE, not re-verified against
  current binary
- `INFERRED` — reconstructed from string/behavioral evidence only, internal
  logic unconfirmed

---

## Principles

- **Binary is ground truth.** If the binary says it, the source must say it.
- **Strings are witnesses.** Every string testifies to a code path. Missing = missing logic.
- **Everything is readable.** Stripped, obfuscated, packed — the CPU reads it, so can you. Read the disassembly.
- **Evidence over opinion.** Log your evidence. Every function cites offsets, strings, or disassembly patterns.
- **Verify differentially.** Compiling is necessary but not sufficient. The output must match in strings, symbols, and behavior.
- **Mark what you can't finish.** `TODO(re):` with description. Unmarked stubs are worse than acknowledged gaps.
- **Iterate.** Verification finds gaps. Return and fill them. Repeat until the string diff is clean.
