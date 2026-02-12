# Remaining Verification Work

See <../README.md> for completed work (binary analysis, decompilation,
translation, build).

## Verification Strategy

### Level 1: String coverage

Extract all application-level strings from both binaries and diff. Every
`[DEBUG]`, `[CONTROL]`, `[SECURITY]`, `[ERROR]`, `[INFO]`, `[OOM_KILL]` format
string in the original must appear verbatim in our reconstruction.

### Level 2: Behavioral testing

Write a WebSocket test harness that exercises the protocol against both the
original and reconstruction:

1. `CreateProcess` -> stdout + `ProcessExited`
2. Wrong `expected_container_name` -> rejection
3. `ProcessConnection` reattach to detached process
4. `SendSignal` to running process -> `SignalSent`
5. Memory hog -> `ProcessOutOfMemory` / `ContainerOutOfMemory`
6. `StdInEOF` -> stdin closes
7. Local IP with `--block-local-connections` -> rejected

### Level 3: Address-level traceability

Every function in the reconstruction has a `/// Decompiled from 0xAAAA..0xBBBB`
comment. This lets anyone verify any function by opening the binary at that
address in Ghidra and comparing the logic.

### Level 4: Section size comparison

Compile with the same rustc (1.83.0) and crate versions. `.text` section size
should be within ~20% of the original.

## Status

- [x] Install Ghidra headless + run full binary analysis
- [x] Generate function catalog (address -> source file mapping via string xrefs)
- [x] Bazel build setup (`BUILD.bazel`, deps in `Cargo.toml`)
- [x] Decompile and translate each module:
  - [x] `pid_tree.rs`
  - [x] `control_server.rs`
  - [x] `cgroup.rs`
  - [x] `state.rs`
  - [x] `adopter.rs`
  - [x] `proc_handle.rs`
  - [x] `oom_killer.rs`
  - [x] `main.rs`
  - [x] `io.rs`
- [x] Full build compiles
- [ ] Verification:
  - [ ] String coverage diff passes
  - [ ] Behavioral test harness written
  - [ ] Behavioral tests pass against both binaries
  - [x] Every function has `Decompiled from 0x...` annotation
