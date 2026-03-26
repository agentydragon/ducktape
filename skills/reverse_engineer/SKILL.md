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

# Go garble?
go version -m "$BINARY" 2>&1 | grep -q unknown && echo "GARBLE-OBFUSCATED"

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

**What's preserved**: all string literals, runtime behavior (CLI, network,
files), serialization formats (protobuf, JSON tags), embedded content
(templates, scripts), syscall patterns, and the full disassembly.

**What's destroyed**: symbol names, DWARF, `go version -m` output, package
paths in type metadata.

**Techniques:**

- **Read the assembly directly.** `objdump -d` works on every binary.
  Obfuscation randomizes names but the instructions are identical. Follow
  call chains from known entry points (e.g., `_start` → `main`). Cross-reference
  string loads to identify what each function does.
- **Runtime probing**: `--help`, `--version`, strace for syscalls, ltrace for
  library calls. CLI help text is always preserved.
- **String analysis**: garble doesn't touch string literals. Extract all
  strings — log messages, error texts, CLI flags, API endpoints, JSON tags,
  gRPC service names, embedded scripts. These are your anchors.
- **Binary diffing** (see below): when a prior unobfuscated version exists,
  use BinDiff/Diaphora to automatically match functions across versions and
  recover original names. This is the highest-leverage technique.
- **Behavioral validation**: run both binaries with identical inputs, compare
  outputs, syscall traces, network traffic. Externally observable behavior
  is ground truth regardless of obfuscation.

### Binary Diffing: Recovering Names from a Prior Version

When you have two versions of the same binary — one with symbols (old) and one
obfuscated (new) — binary diffing tools can automatically match functions across
versions and transfer names from old→new. This is dramatically more efficient
than manual RE of the obfuscated binary.

**Tools (in order of preference):**

1. **BinDiff** (Google, free) — Ghidra or IDA plugin. Matches functions by
   call graph structure, string references, basic block count, instruction
   mnemonics. Exports a SQLite database of matched pairs.
2. **Diaphora** (open source) — Ghidra/IDA plugin. Similar matching but also
   compares pseudo-code ASTs. Better at partial matches.
3. **radiff2** (radare2) — CLI-based binary diffing. Lighter weight, scriptable.
4. **Custom string-anchored matcher** — when tools aren't available, a script
   can match functions by their string reference sets (see below).

**Workflow with BinDiff/Diaphora:**

```bash
OLD_BINARY="staging-with-symbols"
NEW_BINARY="release-garbled"

# 1. Import both into Ghidra projects (headless)
analyzeHeadless /tmp/ghidra_old OldProject -import "$OLD_BINARY"
analyzeHeadless /tmp/ghidra_new NewProject -import "$NEW_BINARY"

# 2. Run BinDiff/Diaphora to produce function mapping
# Output: pairs of (old_name, old_addr) → (new_addr, similarity_score)

# 3. Filter by confidence
#    similarity > 0.9 → high confidence, transfer name directly
#    similarity 0.7-0.9 → verify string refs match before transferring
#    similarity < 0.7 → manual review required

# 4. Apply to RE: for each matched pair, update address annotations
#    in the reconstructed source from old_addr → new_addr
```

**String-anchored matching (no tools required):**

When BinDiff/Diaphora aren't available, you can match functions by their
string references alone. Most application functions reference at least one
unique string (log message, error text, format string). Functions referencing
the same set of strings are the same function.

```bash
OLD="$OLD_BINARY"
NEW="$NEW_BINARY"

# 1. For old binary (has symbols): map function → strings it references
#    Use: go tool objdump -s 'pkg.Func' "$OLD" | grep string offsets
#    Or: Ghidra xref analysis

# 2. For new binary: find all string-referencing code sites
#    strings -t x "$NEW" gives (offset, string) pairs
#    objdump -d "$NEW" | grep -B5 <string_offset> finds the referencing function

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

**Go-specific: garble preserves struct tags.**

Garble randomizes symbol names but preserves `json:"field_name"` struct tags
and other string metadata. Extract with:

```bash
strings "$BINARY" | grep -oP 'json:"[^"]*"' | sort -u
```

These reveal the complete wire format (JSON field names, omitempty flags) of
every serialized struct, even without symbols. Combined with the function
mapping, you can recover the full type system.

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
