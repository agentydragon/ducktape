---
name: reverse_engineer
description: Systematic binary reverse engineering toolkit. Extract source code, understand functions, document protocols, compare versions. Uses strings, symbols, disassembly, and differential verification.
argument-hint: "<path/to/binary> [language]"
---

# Reverse Engineering Toolkit

**Argument:** `$ARGUMENTS` — path to binary (or `.gz`-compressed), optionally target language.

**The binary is ground truth.** Every decision traces to binary evidence — strings, disassembly, syscalls, or runtime behavior. Your opinions about how code "should" look are irrelevant when they contradict the binary.

**Every binary is reversible.** Stripping removes metadata, not behavior. Obfuscation renames symbols, not logic. The CPU executes the same instructions regardless of whether you have debug info. You always have: the full instruction stream, string literals, the syscall interface, and runtime behavior. Adjust techniques, not ambition.

**Aim for the perfect artifact; track everything not yet done.** When the goal is full-equivalence recovery, the target is one source tree that is correct everywhere — same protocol, same crypto, same constants, same edge cases. There is no "good enough." Take well-reasoned steps; a careless inference made 30 minutes ago compounds into hours of downstream debugging. Maintain explicit accounting of what's still in flight: a TODO list in `/work/`, the string checklist from Census updated as code lands, the set of binary symbols not yet matched to recovered functions. Stubs and guesses are fine and necessary early on — but they **MUST** be labeled as such _in the code itself_ with comments like `// GUESS: not yet RE'd from the binary` or `// STUB: returns plausible default; needs confirmation`. The marker IS the bookkeeping. A guess left unmarked is a guess that turns into "fact" the next time you (or anyone else) reads the file. A recovery that "works for the happy path" is a recovery whose un-reverse-engineered parts you forgot about — not a recovery that's done.

**When a tool fails, diagnose and build.** A tool returning an error or empty output is not a dead end — it is a precise description of what the binary violates. Read the error, find the exact check that failed, determine what the binary does instead, and write the minimal fix. Add the fix to `examples/` and record it in the Defeating Obfuscation section below. This skill is a living document: every obfuscation technique you defeat belongs here so the next run starts ahead.

**No speculation. Read it or test it.** Every line of recovered source must trace to bytes you actually read or behavior you actually observed. The signal that your recovery is correct has to be _causally entangled_ with the binary itself — bytes produced by the binary, or bytes read from the binary — not just internally consistent. A cipher that round-trips its own output is consistent with infinitely many wrong implementations; only matching ciphertext the binary itself produced confirms it. When a step would require you to guess, stop: either read the bytes that would tell you, or run the binary to produce a ground-truth observation.

**Expand from understood islands; don't reverse the whole binary in one shot.** Pick one location where you have ground truth — a syscall with known semantics, a recognized constant or magic number, a string with an obvious referent, a function you've already fully reversed — and reverse it to your satisfaction. Then follow data and control flow outward to the adjacent piece, anchored in what you already understand. Each step grows the fully-understood region by one location. The opposite anti-pattern is trying to infer the whole protocol or algorithm in one pass from a distance — e.g. reading the strings table and _guessing_ "I see `register`, `note`, `export`, so the protocol probably looks like X" without having reversed any handler. That produces a recovery that's plausible from far away and falls apart on contact with bytes. Slow expansion from solid ground beats sweeping inference every time, and you can always sweep first to _prioritize_ islands — but the islands are what you actually trust.

**Heuristic red flags mean you mis-RE'd something. Diagnose, don't reroll.** Trust _any_ signal that your recovery cannot possibly be correct — the class is broad and worth thinking through deliberately: behavioral divergence between recovered code and the binary on the same input; internal inconsistencies in the recovery (struct fields that don't line up, constants that don't match across call sites, code paths that can't both be true); smells real production code wouldn't have (dead code, unused variables, vestigial branches, fields nothing reads); uncovered evidence (strings or exported symbols in the binary that the recovery never references, callsites pointing at functions you never characterized); and so on. All of them mean the same thing: the binary is right and your code is wrong at a specific point — by definition, since you recovered the code from the binary. Do not start trying alternative whole implementations: a couple of hypotheses in, you are taking shots in the dark, which is wasted motion when the literal binary is sitting right there available for taking apart. Localize the bug instead. Take the binary apart at the suspect boundary, compare its intermediate state against your code's, and bisect down to the first step that disagrees. Tools you have: `gdb` breakpoints with register/memory dumps, single-stepping with a disassembler, `ltrace`/`strace`, instrumenting your own code to print intermediate state, side-by-side disassembly-vs-source review. The artifact is available; use it.

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

# Go garble -literals? A binary whose own --help text is absent from `strings`
# has encrypted literals; recover them from a core dump (see Defeating Obfuscation).
"$BINARY" --help 2>/dev/null | head -1 | grep -qf - <(strings "$BINARY") \
  || echo "GARBLE -literals (string constants encrypted)"

# Go: enumerate functions from pclntab (non-garbled only — garble XORs the magic)
# Install: go install github.com/goretk/redress@latest
redress packages "$BINARY"      # packages (returns empty on garbled binaries)
# For garbled binaries, rebuild the symbol table first (examples/gosymtab.go):
#   gosymtab "$BINARY" "$BINARY.sym" && go tool nm "$BINARY.sym"

# Application string count
strings "$BINARY" | grep -cE '(error|fail|flag|usage|http|/v[0-9])'
```

| Class          | Symbols | DWARF | Approach                                          |
| -------------- | ------- | ----- | ------------------------------------------------- |
| **Unstripped** | Yes     | Yes   | DWARF + symbol extraction → source reconstruction |
| **Stripped**   | No      | No    | String-anchored Ghidra/objdump decompilation      |
| **Obfuscated** | Mangled | No    | Disassembly + runtime probing + string analysis   |

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

| Technique                                       | What breaks                                                                                       | Fix                                                                                                                                  | File                   |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ---------------------- |
| garble XORs `.gopclntab` magic bytes (v0.13.0+) | `go tool objdump`, `redress`, `GoReSym`, `debug/gosym` all fail to parse pclntab                  | `pclntool patch` writes a repaired binary; `pclntool pc` maps a PC to its garbled fn                                                 | `examples/pclntool.go` |
| garble strips ELF `.symtab`                     | `go tool objdump`/`nm` fail with "no symbol section"                                              | `gosymtab` **synthesizes a real `.symtab`** from pclntab, restoring the whole standard toolchain (see below)                         | `examples/gosymtab.go` |
| `.text` does not start at the first Go function | Every symbol recovered from pclntab is shifted; disassembly lands mid-instruction                 | `gosymtab` scores both candidate text bases against real prologue bytes and picks the winner (see below)                             | `examples/gosymtab.go` |
| garble `-literals` encrypts string constants    | Help text, metric names, endpoints absent from `strings`; no static string anchors                | Dump process memory after package init and read the decrypted literals out of a core (see below)                                     | (recipe below)         |
| garble `-literals` hides function-local strings | A local literal never decrypts unless its function runs, and running it means running the feature | The keys are binary-resident: emulate the decryptor over the mapped image (unicorn). Live-process probe only as fallback (see below) | (recipe below)         |
| garble randomizes struct and field names        | `strings`-scraped tags lose which struct they belong to, plus field order and types               | `gotypes` walks `.typelink`/`abi.Type` to recover full struct definitions with offsets (see below)                                   | `examples/gotypes.go`  |
| garble strips module info from `.go.buildinfo`  | `go version -m` returns `unknown`                                                                 | Use Go compiler version from `redress info` or `readelf` on `.go.buildinfo` section                                                  | (no file needed)       |

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
byte offset → VMA → instruction PC → function name (see `examples/binary_diff_recipe.sh`).

**Restore the whole toolchain with `gosymtab`.** Rather than working around the
missing `.symtab`, rebuild it. `examples/gosymtab.go` reads the (magic-repaired)
pclntab, enumerates every function, and writes a genuine ELF `.symtab`/`.strtab`
into a copy of the binary:

```bash
gosymtab ./garbled ./garbled.sym
go tool nm ./garbled.sym                          # all functions, garbled names
go tool objdump -s '^main\.main$' ./garbled.sym   # CALLs resolve to names again
```

This is strictly better than falling back to GNU `objdump -d`: `go tool objdump`
understands Go's calling convention and annotates call targets, and gdb, radare2
and Ghidra all pick the symbols up automatically. On `environment-manager` it
recovered 46,573 functions. `//skills/reverse_engineer/examples:test_gosymtab`
verifies it end to end against a garbled fixture.

**Watch the text base.** `debug/gosym` ignores the pclntab header's `textStart`
for the Go 1.18/1.20 table formats and trusts the caller's value instead
(the comment in Go's source says it "may be unrelocated"). Every tool built on
it therefore passes the `.text` section address — which is wrong whenever the
linker places an entry stub or padding before the first Go function. On
`environment-manager`, `.text` starts at `0x4023a0` but the first Go function is
at `0x4024a0`, so every recovered symbol was shifted by 0x100: names still
appear, disassembly still renders, and everything is quietly off by one
function. Detect it rather than trusting either value — a correct base lands
function entries on real prologues (`CMPQ SP,0x10(R14)` or `PUSHQ BP; MOVQ SP,BP`),
a wrong one lands them mid-instruction. `gosymtab` scores both candidates and
picks the winner; the two separated 77.3% vs 37.0% here, which is not a close call.

This is the failure mode the "heuristic red flags mean you mis-RE'd something"
principle exists for: the tell was that the ELF entry point fell _inside_ a
function rather than at its start.

### Go-Specific: Instruction-Level Analysis and String-to-Function Anchoring

`go tool objdump` requires an ELF symbol table and fails on garbled binaries
(`no symbol section`). `go tool addr2line` similarly fails on garbled binaries
(returns `?` for all addresses). Use `objdump -d` (GNU binutils) for disassembly.
For pclntab-based PC→function mapping, use `pclntool` (see `examples/pclntool.go`):
garble v0.13.0+ obfuscates the `.gopclntab` magic bytes, so standard Go tooling
that reads pclntab won't work until the magic is repaired.

**Workflow:** read `examples/binary_diff_recipe.sh` — a runnable demonstration
with inline commentary explaining each step.

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

**Recovery via core dump (verified — use this first).**

Don't fight the decryption; let the binary do it and then read its memory. Go
initialises every imported package at startup, so by the time the process is
about to exit, every package-level string constant has already been decrypted
somewhere in the heap or BSS:

```bash
gdb -batch -nx -ex 'set pagination off' -ex 'set confirm off' \
    -ex 'catch syscall exit_group' -ex 'run' -ex 'generate-core-file core' \
    --args ./binary --help
strings -n 4 core | sort -u > decrypted.txt
comm -13 <(strings -n 4 ./binary | sort -u) decrypted.txt > encrypted-only.txt
```

`encrypted-only.txt` is exactly the set `-literals` was hiding. On
`environment-manager` this recovered 53,703 strings, including every cobra help
string, all OpenTelemetry metric names and descriptions, and internal literals
like `GetClaudePath` that are never printed — proof the technique recovers
program constants and not merely echoed output.

Choose the driving subcommand for coverage: `--help` is side-effect free and
initialises all packages, so it gets every **package-level** literal. Literals
local to a function body are only decrypted when that function runs, so a
function you never execute keeps its secrets. Union several safe invocations
when you need more coverage.

**Keep the addresses, not just the text.** A flat `strings` dump of the core
tells you _what_ the literals are but not _where_ they are used, which is the
half you need for cross-referencing. Recover the mapping instead: generated
`init()` code decrypts package-level literals into heap buffers whose
`(ptr, len)` string headers are stored into globals in `.data`/`.bss`. Scan that
range in the core, treat each 16-byte pair as a candidate header, resolve `ptr`
in the core's memory, and keep it when the bytes are printable:

```python
# for each 8-byte-aligned offset in the binary's .data/.bss VMA range:
ptr, ln = struct.unpack_from("<QQ", region, off)
if 2 <= ln <= 400 and ptr >= 0x400000:
    s = read_from_core(ptr, ln)          # None if unmapped
    if s and all(0x20 <= c < 0x7f or c in (9, 10, 13) for c in s):
        globals_[region_base + off] = s.decode()
```

That yields `global_addr -> plaintext`, so any instruction RIP-referencing the
global is a use of that literal and static cross-referencing works again. On
`environment-manager` this resolved 9,203 globals.

**Function-local literals: emulate the decryptor, don't run it.** Locals stay
encrypted unless their function executes, and you often cannot run the function
— it is the feature you are trying to understand, and running it has side
effects.

Reach for a live process last, not first. A garble decryptor is straight-line
arithmetic over a buffer, and **its keys are binary-resident**: verified on a
real target where the key came through a three-deep pointer chain
(`global → +0x30 → +0x30`) that looked like runtime state but resolved entirely
inside `.data`. So map the binary's `PT_LOAD` segments into a CPU emulator
(unicorn), give it a stack, point `R14` at a zeroed page so goroutine-relative
reads return 0 instead of faulting, and run the block:

```python
for seg in elf.iter_segments():           # map the image
    if seg['p_type'] == 'PT_LOAD':
        uc.mem_map(align_down(seg['p_vaddr']), ...); uc.mem_write(...)
uc.reg_write(UC_X86_REG_RSP, STACK)
uc.reg_write(UC_X86_REG_R14, ZEROED_PAGE)
uc.emu_start(block_start, block_end)
buf = uc.mem_read(STACK + frame_off, n)   # the plaintext
```

Skip `CALL`s rather than emulating the Go runtime — hand back a scratch buffer
in `RAX` so a heap-allocating decryptor has somewhere to write. On the target,
the emulated key registers matched the statically-computed values exactly, which
is the check to run before trusting any recovered plaintext.

**The obstacle is control flow, not runtime state.** Garble's _split_ obfuscator
scatters a single literal's byte operations across the whole containing
function, interleaved with unrelated code, so there is no self-contained block
to emulate — you have to cover every path that touches the buffer, and those
paths may depend on inputs you do not have. That, not key secrecy, is what makes
some literals hard.

Only when static emulation cannot reach a literal is it worth attaching to a
live process: freeze it at a safe breakpoint, point `$pc` at the decryption
block's buffer allocation, run to the `[]byte`→`string` conversion, and read the
result — with `GOGC=off GODEBUG=asyncpreemptoff=1` so the GC and the preemption
signal handler cannot move or interrupt the buffer mid-probe. It is the more
invasive option and it needs a runnable binary, so treat it as the fallback.

Either way, treat a literal you could not decrypt as unknown and mark it — do
not infer it from the surrounding code.

Two practical notes: the core can be far larger than the binary (~1.8 GB for a
59 MB input), so extract strings and delete it immediately; and garble's
decrypted buffers often sit adjacent in memory, so a recovered "string" may run
into its neighbour — treat exact matches as reliable and long concatenations as
fragments to be split.

**Recovery via disassembly** (fallback, when the binary cannot be run):

Each encrypted string has generated code that decrypts it in place using
XOR/shift sequences over a stack buffer — look for runs of `MOVB $imm, n(SP)`
followed by `XORB`/`ADDB`/`ROLB` over the same slots, then emulate the sequence.
Prefer the core-dump route: it is faster and yields the whole set at once.

**Go-specific: garble preserves struct tags.** Even with `-literals`, garble
cannot encrypt `json:"field_name"` struct tags because the `encoding/json`
package reads them via reflection at runtime. Extract all tags:

```bash
strings "$BINARY" | grep -oP 'json:"[^"]*"' | sort -u
strings "$BINARY" | grep -oP '[a-z_]+,omitempty' | sort -u
```

These reveal that a wire format exists, but not its shape — a flat list of tags
loses which struct each belongs to, the field order, the Go types, and every
untagged field. Recover the actual definitions instead with `gotypes`
(`examples/gotypes.go`):

```bash
gotypes ./binary                     # every struct: fields, types, offsets, tags
gotypes -filter Config ./binary      # narrow by type name
```

It walks `.typelink` into `abi.Type` and expands composite types recursively.
The runtime needs this metadata for interface dispatch, type assertions and
reflection, so the linker cannot drop it and garble cannot rewrite it away.
Field _names_ are garbled, but the tags and the byte offsets are exact, which
pins the wire format and the memory layout:

```text
type main.QsttgfCY661L struct {
        FeLwnu4WC    string  `json:"host"`             // +0x0
        FfE93J5NPav  int     `json:"port"`             // +0x10
        Uk3WI1       string  `json:"token,omitempty"`  // +0x18
}
```

Because the output is structural, it also **diffs across builds**: dumping two
versions and comparing gives an exact protocol delta — fields added, removed, or
moved — where a `strings` diff only shows tags appearing and disappearing with
no idea which struct changed. On a real target this distinguished "four fields
were removed" from what had actually happened: one nested struct was deleted
whole.

**Gotcha: the types base is not `.rodata`'s address.** `.typelink` holds offsets
relative to `moduledata.types`, which sits _near_ the start of `.rodata` but not
at it — on a real binary they differed by 0x20. Using the section address still
produces plausible-looking output (a few valid types, then garbage), which is
the dangerous failure mode. `gotypes` scores candidate bases by how many
`.typelink` entries decode into a sane type and reports the winner; the wrong
base yielded 164 structs, the right one 3,014. Override with `-types-base` if
the detection ever picks wrong.

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

**See `examples/binary_diff_recipe.sh` for a runnable, CI-verified demonstration**
of this technique: plain v1 binary (symbols intact) vs. garble-obfuscated v2
(new function added). The recipe shows shared-string matching identifying the
same function across versions, and v2-only strings identifying the new function.

**What to do with the mapping:**

- Update all address annotations in RE source: `old_addr → new_addr`
- For matched functions with high confidence: mark as `VERIFIED`
- For unmatched functions in the new binary: these are new code — prioritize
  for manual RE
- For functions in old but not new: removed code — delete from RE
- For functions with partial matches (same strings but different size):
  changed logic — compare disassembly and update RE

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

**No `#[allow(dead_code)]` / `// nolint` suppression.** These mean you failed to find the caller.

## Confidence Levels

When documenting RE provenance, mark each module/function:

- `VERIFIED` — behavior confirmed against the target binary (runtime test, string match, or disassembly match)
- `CARRIED` — copied from a prior version's RE, not re-verified against current binary
- `INFERRED` — reconstructed from string/behavioral evidence only, internal logic unconfirmed

## Principles

- **Binary is ground truth.** If the binary says it, the source must say it.
- **Strings are witnesses.** Every string testifies to a code path. Missing = missing logic.
- **Everything is readable.** Stripped, obfuscated, packed — the CPU reads it, so can you. Read the disassembly.
- **Evidence over opinion.** Log your evidence. Every function cites offsets, strings, or disassembly patterns.
- **Verify differentially.** Compiling is necessary but not sufficient. The output must match in strings, symbols, and behavior.
- **Mark what you can't finish.** `TODO(re):` with description. Unmarked stubs are worse than acknowledged gaps.
- **Iterate.** Verification finds gaps. Return and fill them. Repeat until the string diff is clean.
