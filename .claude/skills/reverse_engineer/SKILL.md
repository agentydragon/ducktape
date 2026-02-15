---
name: reverse_engineer
description: Systematic binary reverse engineering toolkit. Extract source code, understand functions, document protocols, compare versions. Uses strings, symbols, disassembly, and differential verification.
argument-hint: "<path/to/binary> [language]"
allowed-tools: Bash, Read, Grep, Glob, Edit, Write, Task, WebFetch
---

# Reverse Engineering Toolkit

Comprehensive guide for reverse engineering binaries. Choose the appropriate techniques based on what the user needs:

- **Full reconstruction:** Complete, compilable source code
- **Function analysis:** Understand how specific parts work
- **Protocol documentation:** API specs and wire formats
- **Version comparison:** What changed between releases
- **Deepening existing RE:** Fill gaps in partial reconstructions

**Argument:** `$ARGUMENTS` — path to the binary (or `.gz`-compressed binary), and optionally the target language (auto-detected if omitted).

**The binary is ground truth.** Every decision must be traceable to binary evidence—strings, symbols, disassembly, or runtime behavior. Your opinions about how code "should" look are irrelevant when they contradict what's in the binary.

**Approach:** Start with Phase 1 (census) for context, then select techniques from subsequent phases based on scope. For full reconstruction, work through all phases. For focused tasks (single function, protocol docs, etc.), use Phase 0.5 techniques to zero in on the target.

---

## Phase 0: Setup and Tool Installation

Install analysis tools as needed. Prefer what's available; install what's missing.

```bash
# Check what's available
which readelf objdump strings nm file ldd strace ltrace 2>/dev/null

# Install essentials if missing
apt-get update && apt-get install -y binutils file strace ltrace

# For deeper analysis (install if needed for complex binaries)
# apt-get install -y ghidra radare2
# pip install capstone ropper
```

If the binary is compressed (`.gz`, `.xz`, `.zst`), decompress it to a temp
location first:

```bash
BINARY="/tmp/target_binary"
# gzip -dk path/to/binary.gz -c > "$BINARY" && chmod +x "$BINARY"
```

## Phase 0.5: Focused Analysis Techniques

Techniques for targeted reverse engineering when you need to understand or document specific parts of a binary without full reconstruction. Use these when the task is scoped to:

- Understanding how a specific function works
- Documenting an API/protocol from a component
- Comparing what changed between binary versions
- Deepening partial/existing reverse engineering
- Answering "how does it do X?" questions

These techniques complement the full reconstruction workflow—use them to zoom in on specific areas.

### 0.5.1 Identify target symbols/functions

```bash
BINARY="/usr/local/bin/target"
TARGET="auth"  # module, function name, or component

# Find all related symbols
nm "$BINARY" | grep -i "$TARGET" > /tmp/${TARGET}_symbols.txt

# Get function addresses and names
nm "$BINARY" | grep -i "$TARGET" | grep ' T ' | awk '{print $1, $3}'

# For unstripped binaries, get source file info
readelf --debug-dump=info "$BINARY" | grep -B5 -A10 "$TARGET"
```

### 0.5.2 Extract relevant strings

Strings reveal behavior - log messages, error paths, endpoints, formats:

```bash
# All strings mentioning the target
strings "$BINARY" | grep -i "$TARGET" > /tmp/${TARGET}_strings.txt

# Categorize by type
strings "$BINARY" | grep -iE "(error|fail).*${TARGET}" > /tmp/${TARGET}_errors.txt
strings "$BINARY" | grep -iE "^/.*${TARGET}" > /tmp/${TARGET}_paths.txt  # URLs/paths
strings "$BINARY" | grep "json:.*${TARGET}" > /tmp/${TARGET}_json.txt   # JSON fields
```

### 0.5.3 Disassemble target functions

Extract implementations of key functions:

```bash
# Get function bounds
FUNC_START=$(nm "$BINARY" | grep "MyFunction" | awk '{print "0x"$1}')
FUNC_END=$(nm "$BINARY" | awk -v start="$FUNC_START" '$1 > start {print "0x"$1; exit}')

# Disassemble
objdump -d "$BINARY" --start-address="$FUNC_START" --stop-address="$FUNC_END" > /tmp/func.asm

# For Go binaries, use go tool objdump for better formatting
go tool objdump -s MyFunction "$BINARY" > /tmp/func.asm
```

### 0.5.4 Analyze data structures

For typed languages (Go, Rust, C++ with debug info):

```bash
# Extract type definitions
readelf --debug-dump=info "$BINARY" | grep -A 50 "DW_TAG_structure_type" | grep -A 50 "$TARGET"

# For Go, look for reflect type metadata
strings "$BINARY" | grep "type\\..*${TARGET}"
strings "$BINARY" | grep "json:.*${TARGET}"  # struct tags reveal wire formats
```

### 0.5.5 Compare binary versions (diff analysis)

When analyzing what changed:

```bash
OLD_BINARY="app-v1.2"
NEW_BINARY="app-v1.3"

# Symbol diff
diff <(nm "$OLD_BINARY" | sort) <(nm "$NEW_BINARY" | sort) > /tmp/symbol_diff.txt

# String diff (reveals new features, changed messages)
diff <(strings "$OLD_BINARY" | sort) <(strings "$NEW_BINARY" | sort) > /tmp/string_diff.txt

# Size diff per section
diff <(readelf -S "$OLD_BINARY") <(readelf -S "$NEW_BINARY")

# For specific function changes, compare disassembly
diff <(objdump -d "$OLD_BINARY" --start-address=0xABCD) \
     <(objdump -d "$NEW_BINARY" --start-address=0xDEF0)
```

### 0.5.6 Output format

Choose output based on the task:

**For "how does function X work?"** → Write a detailed explanation with:

- Purpose (inferred from strings, call sites, context)
- Algorithm (from disassembly/decompilation)
- Error paths (from error strings)
- Dependencies (from calls to other functions)

**For "document API/protocol"** → Create specification with:

- Endpoints/interfaces (from strings, URL construction)
- Request/response formats (from JSON tags, marshal/unmarshal code)
- Authentication (from header-setting code)
- Examples (verified against live API if possible)

**For "what changed between versions?"** → Write a changelog with:

- New functions/symbols
- Modified functions (with before/after behavior)
- Removed functionality
- Changed constants/strings

**For "deepen existing RE"** → Add to existing reconstruction:

- Implement previously stubbed functions
- Add missing error paths
- Clarify ambiguous logic
- Verify against binary

---

## Phase 1: Binary Census

**Do not write ANY source code until this phase is complete.** This phase
produces a written inventory that governs all subsequent work.

### 1.1 File identification

```bash
file "$BINARY"
readelf -h "$BINARY"       # ELF header: arch, endianness, entry point
readelf -d "$BINARY"       # dynamic section: linked libraries
readelf -n "$BINARY"       # build ID, notes
```

Determine:

- **Language**: Rust (look for `rust_begin_unwind`, `core::fmt`, mangled
  `_ZN` symbols with `h` hash suffixes), Go (`runtime.main`, `go.buildid`),
  C/C++ (standard vtable patterns, `__cxa_throw`), etc.
- **Compiler version**: Rust embeds version strings; Go embeds `go1.x`;
  GCC/Clang often visible in `.comment` section
- **Static vs dynamic linking**
- **Stripped vs unstripped** (`readelf -s` for symbol table)

### 1.2 Complete string extraction

This is the single most important step. Every string in the binary witnesses a
code path that MUST exist in your source.

```bash
strings -n 6 "$BINARY" | sort -u > /tmp/binary_strings_all.txt
wc -l /tmp/binary_strings_all.txt

# Categorize strings
strings -n 6 "$BINARY" | grep -i '\[debug\]\|error\|fail\|warn' > /tmp/strings_log.txt
strings -n 6 "$BINARY" | grep -iE '^\/' > /tmp/strings_paths.txt
strings -n 6 "$BINARY" | grep -E '\.(rs|go|py|c|cpp|h)' > /tmp/strings_source_refs.txt
strings -n 6 "$BINARY" | grep -iE 'http|socket|addr|port|listen|connect' > /tmp/strings_network.txt
strings -n 6 "$BINARY" | grep -iE 'usage|help|version|flag|arg|option' > /tmp/strings_cli.txt
```

### 1.3 Symbol analysis

```bash
# Dynamic symbols (even stripped binaries have these)
nm -D "$BINARY" 2>/dev/null > /tmp/dynamic_symbols.txt
readelf --dyn-syms "$BINARY" > /tmp/dynsym.txt

# Full symbol table (if not stripped)
nm "$BINARY" 2>/dev/null > /tmp/symbols.txt

# Imported libraries and functions
ldd "$BINARY" 2>/dev/null
readelf -d "$BINARY" | grep NEEDED
```

### 1.4 Section analysis

```bash
readelf -S "$BINARY"           # all sections with sizes
readelf -p .rodata "$BINARY"   # read-only data (constants, string literals)
readelf -p .comment "$BINARY"  # compiler info
objdump -s -j .rodata "$BINARY" | head -200  # hex dump of rodata
```

### 1.5 Disassembly (selective)

Full disassembly of large binaries is impractical. Target specific areas:

```bash
# Entry point and main
objdump -d "$BINARY" | grep -A 50 '<main>'
objdump -d "$BINARY" | grep -A 50 '<_start>'

# Function list (from symbols if available)
objdump -t "$BINARY" | grep ' F ' | sort -k5 -n -r | head -50  # largest functions

# Cross-reference: find code that references a specific string
# 1. Find string offset in .rodata
strings -t x "$BINARY" | grep "target string"
# 2. Search disassembly for references to that offset
```

### 1.6 Produce the inventory document

Before proceeding, write a structured inventory (as a comment block or separate
file) containing:

1. **Binary metadata**: arch, language, compiler version, linking, stripped?
2. **String checklist**: every application-level string (not stdlib/compiler
   noise), each marked as UNCOVERED. This checklist is updated throughout
   reconstruction. A string is COVERED when source code containing it is written.
3. **Dependency list**: external crates/packages/libraries with versions
   (inferred from strings, symbol names, `.comment` section)
4. **Module structure hypothesis**: based on source file path strings
   (e.g., `src/main.rs`, `src/io.rs`) and functional groupings
5. **Function inventory**: known functions with approximate sizes, grouped by
   module

## Phase 2: Project Skeleton

### 2.1 Reconstruct the build configuration

- **Rust**: `Cargo.toml` with dependencies inferred from Phase 1. Match
  versions from embedded strings (e.g., `tokio-1.38.0` in panic messages).
  Or, if using Bazel, the appropriate `BUILD.bazel` + `Cargo.toml`.
- **Go**: `go.mod` with dependencies from embedded module paths.
- **C/C++**: `Makefile`/`CMakeLists.txt` with library flags from `ldd` output.

### 2.2 Create module files

Based on source path strings from the binary (e.g.,
`/build/src/control_server.rs` reveals a module named `control_server`).
Create empty files with doc comments recording:

- Binary offset range for functions in this module (if determinable)
- String references that belong to this module

### 2.3 String coverage tracking

Maintain a checklist (in a tracking file or structured comments) mapping every
application string to its source location. Format:

```
[x] "Failed to bind" -> src/main.rs:bind_listener()
[ ] "Invalid UTF-8 in request body" -> UNCOVERED
[x] "[DEBUG] Cgroup setup successful" -> src/cgroup.rs:setup_cgroup()
```

Update this as you write each function. Phase 4 verification will catch any
you missed, but proactive tracking is faster.

## Phase 3: Function-by-Function Reconstruction

Work through the function inventory from Phase 1. For each function:

### 3.1 Anchor on strings

Every string reference in the function is a structural anchor. The strings
dictate:

- What error paths exist
- What log messages are emitted
- What CLI flags/help text is defined
- What file paths are accessed

### 3.2 Trace control flow

From disassembly or decompiler output around string references:

- Identify branch conditions (what causes each error message)
- Identify loops (retry patterns, polling, iteration)
- Identify function calls (callees and their signatures)
- Identify resource lifecycle (open/close, alloc/free, lock/unlock)

### 3.3 Write the source

Write the function with:

- A doc comment citing binary evidence (offset, string refs)
- The exact string literals from the binary (character-for-character)
- Control flow matching the binary's structure
- Error handling matching every error string

### 3.4 Mark coverage

After writing each function, update the string checklist. Flag any strings you
couldn't place — they indicate missing code paths.

### 3.5 Mark incomplete reconstructions

Not every function can be fully reconstructed in a single pass. When you cannot
fully recover a function body, closure, type, or control flow path, you MUST
mark it with a `TODO(re):` comment so it's greppable and clearly incomplete.

**Required markers (use exactly `// TODO(re):` or `# TODO(re):` prefix):**

- **Stub function bodies**: `// TODO(re): stub — <what the function should do>`
- **Empty goroutine/closure bodies**: `// TODO(re): stub — <describe the closure's purpose from binary evidence>`
- **Placeholder types** (`interface{}`, `any`, `object` where concrete type
  exists): `// TODO(re): concrete type not recovered — likely <best guess>`
- **Discarded values** (`_ = expr` where the value is clearly used by the real
  binary): `// TODO(re): should be <how the value is consumed>`
- **Commented-out code** standing in for unrecovered logic:
  `// TODO(re): not reconstructed — <brief description of what binary does>`
- **Hardcoded placeholders** (zero values, empty strings, dummy data where the
  binary has real logic): `// TODO(re): placeholder — <what should be here>`
- **Incomplete error handling** (errors swallowed or ignored where the binary
  handles them): `// TODO(re): error handling not reconstructed`

**Every `TODO(re):` must include a brief description** of what the correct
implementation should do, based on binary evidence. Bare `// TODO` without
context is not acceptable.

**Do not leave unmarked stubs.** A function that returns `nil` where the binary
has real logic, a closure with an empty body, or a variable discarded with
`_ =` where the binary consumes it — all of these are reconstruction bugs if
left unmarked. The marker makes the gap visible and searchable.

### 3.6 No dead code

Dead code does not exist in an optimized compiled binary. If you wrote code
that nothing calls, your reconstruction is wrong — find the caller.
`#[allow(dead_code)]`, `#pragma unused`, or equivalent suppressions are
**forbidden**. They mean you gave up finding the call site.

## Phase 4: Differential Verification

After the source compiles, verify against the reference binary.

### 4.1 String diff (mandatory, non-negotiable)

```bash
# Extract strings from YOUR compiled binary
strings -n 6 "$YOUR_BINARY" | sort -u > /tmp/my_strings.txt

# Extract strings from the REFERENCE binary
strings -n 6 "$REFERENCE_BINARY" | sort -u > /tmp/ref_strings.txt

# Strings in reference but missing from yours = missing code paths
comm -23 /tmp/ref_strings.txt /tmp/my_strings.txt > /tmp/missing_strings.txt

# Strings in yours but not in reference = extra/wrong code
comm -13 /tmp/ref_strings.txt /tmp/my_strings.txt > /tmp/extra_strings.txt
```

Every entry in `missing_strings.txt` (excluding stdlib/compiler noise) is a
reconstruction bug. Fix before proceeding.

### 4.2 Symbol diff

```bash
nm -D "$YOUR_BINARY" 2>/dev/null | sort > /tmp/my_dynsym.txt
nm -D "$REFERENCE_BINARY" 2>/dev/null | sort > /tmp/ref_dynsym.txt
diff /tmp/ref_dynsym.txt /tmp/my_dynsym.txt
```

Dynamic symbol mismatches indicate wrong dependency versions or missing
functionality.

### 4.3 Behavioral diff (when possible)

```bash
# Compare syscall sequences with identical inputs
strace -f -o /tmp/ref_strace.txt "$REFERENCE_BINARY" <test_args> &
strace -f -o /tmp/my_strace.txt "$YOUR_BINARY" <test_args> &

# Compare: same files opened? same sockets? same signals handled?
diff <(grep -E 'open|socket|bind|listen|connect|signal' /tmp/ref_strace.txt) \
     <(grep -E 'open|socket|bind|listen|connect|signal' /tmp/my_strace.txt)
```

### 4.4 Section size comparison

```bash
# Compare section sizes — large discrepancies indicate missing/extra code
readelf -S "$REFERENCE_BINARY" | grep -E '\.text|\.rodata|\.data' > /tmp/ref_sections.txt
readelf -S "$YOUR_BINARY" | grep -E '\.text|\.rodata|\.data' > /tmp/my_sections.txt
diff /tmp/ref_sections.txt /tmp/my_sections.txt
```

### 4.5 Stub scan (mandatory before completion)

Before declaring reconstruction complete, scan for remaining incomplete work:

```bash
# Find all TODO(re) markers — every one is an acknowledged gap
grep -rn 'TODO(re)' src/ | tee /tmp/todo_re.txt
wc -l /tmp/todo_re.txt

# Find potential unmarked stubs
grep -rn '_ = ' src/ | grep -v 'TODO' | tee /tmp/unmarked_discards.txt
grep -rn 'interface{}' src/ | grep -v 'TODO' | tee /tmp/unmarked_interfaces.txt
grep -rn '// Stub' src/ | grep -v 'TODO' | tee /tmp/unmarked_stubs.txt
```

**Resolution requirements:**

- Every `_ = expr` that discards a meaningful value must either be fixed
  (value consumed correctly) or marked with `TODO(re):`.
- Every `interface{}` that stands in for a concrete type must either be
  replaced with the correct type or marked with `TODO(re):`.
- Every function/closure with a stub body must be either reconstructed or
  marked with `TODO(re):`.
- The `TODO(re)` count should be documented in the README or inventory so
  the scope of remaining work is visible.

This scan catches gaps that slipped through Phase 3 without markers. It is
non-negotiable — unmarked stubs are worse than marked ones because they look
like intentional implementations.

## Principles

- **Binary is ground truth.** If the binary says it, the source must say it.
- **Strings are witnesses.** Every string in the binary testifies to a code
  path. Missing strings = missing logic. Extra strings = wrong logic.
- **No dead code.** The compiler already removed dead code. Everything in the
  binary is reachable. Find the call path.
- **No warning suppression.** `#[allow(dead_code)]`, `// nolint`, `#pragma`
  suppression = you failed to reconstruct the call graph. Fix the graph.
- **Evidence over opinion.** Log your evidence. Every function should cite
  which binary offsets, strings, or disassembly patterns informed it.
- **Verify differentially.** Compiling is necessary but not sufficient.
  The output binary must match the reference in strings, symbols, and behavior.
- **Iterate.** Phase 4 will find gaps. Return to Phase 3 and fill them.
  Repeat until the string diff is clean.
- **Mark what you can't finish.** Use `// TODO(re): <description>` for any
  stub, placeholder type, discarded value, or unrecovered logic. Unmarked
  stubs masquerade as correct implementations and are worse than acknowledged
  gaps. The `TODO(re):` prefix is greppable and distinguishes reconstruction
  gaps from normal development TODOs.
