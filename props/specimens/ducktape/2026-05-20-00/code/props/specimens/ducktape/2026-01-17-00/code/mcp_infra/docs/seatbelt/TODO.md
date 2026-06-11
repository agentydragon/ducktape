# SBPL Library TODO / Potential Extensions

Status: living list of gaps and candidates for future work. Focus is on `adgn.seatbelt` (model/compile/validate/runner) and closely related tooling.

## Path Filters & Predicates

- [ ] Support additional SBPL path predicates beyond `literal` and `subpath` (e.g., `regex`, `home-literal`, `home-subpath`).
- [ ] Optional `vnode-type` predicate support for finer file rules (regular, dir, symlink, socket, etc.).
- [ ] Path normalization utilities and overlap detection (dedupe when `subpath("/a")` covers `literal("/a/file")`).
- [ ] Configurable parent-directory `file-read-metadata` expansion helper (builder-level, not in the compiler core).

## Boolean Composition & Macros

- [ ] Filter composition (logical and/or/not) for complex conditions where SBPL supports it.
- [ ] Macro/`define` support for reusable groups (kept explicit; no hidden defaults).
- [ ] Optional include/import of fragments (controlled; disabled by default).

## Operations Coverage (SBPL Surface)

- [ ] File ops beyond current subset: finer-grained `file-read-data`, `file-read-xattr`, `file-write-xattr`, `file-rename`, `file-unlink`, etc.
- [ ] Additional system ops: `sysctl-write`, other `system-*` toggles commonly used in Apple profiles.
- [ ] Mach: `mach-register` and other Mach message/port operations, in addition to existing `mach-lookup`.
- [ ] POSIX/IPC scopes (where applicable and stable): shared memory, semaphores, message queues, etc.
- [ ] IOKit and device access toggles where SBPL exposes safe, documented predicates.

## Network Predicates

- [ ] Extend network rules with remote IP/port predicates (ingress/egress scoping), protocol scoping (tcp/udp), and richer loopback options.

## Compiler Enhancements

- [ ] Strictly magic-free compile: remove or make optional the current implicit write for trace path. (See `compile_sbpl()` trace block.)
- [ ] Pretty/compact formatting options (indentation, grouping by op), stable sorting toggles while preserving caller order when desired.
- [ ] Robust SBPL quoting/escaping (backslashes, quotes, non‑ASCII, control chars) with round‑trip tests.
- [ ] Emit comments (optional) for readability in generated profiles.

## Validation Improvements

- [ ] Structured findings: codes, severity, categories, and suggested fixes (not just strings).
- [ ] Must‑fix vs warn classification, with an option to raise on must‑fix.
- [ ] Path hygiene: absolute‑path enforcement, `~`/relative rejection (message now; add targeted checks).
- [ ] Overlap analysis and rule‑count guardrails (warn on pathological policies).
- [ ] macOS compatibility matrix and heuristic checks (dyld/stdlib coverage, known abort patterns) with OS‑specific guidance.

## Runner & Tooling

- [ ] Synchronous convenience wrapper around async APIs (ergonomics only).
- [ ] Configurable artifacts directory and retention policy; consistent trace/unified‑log collection toggles.
- [ ] Optional unified log harvest when exit!=0 (currently disabled by default) with safe time windows.
- [ ] CLI (`python -m adgn.seatbelt`) with subcommands: `validate`, `compile`, `run` (thin wrapper around the library).

## Presets / Builders

- [ ] Provide explicit builders for common scenarios (opt‑in):
  - [ ] Minimal Python runtime (dyld roots, venv/bin/lib, device basics, loopback net).
  - [ ] Jupyter kernel sandbox presets (tunable read/write mounts, loopback).
  - [ ] “Echo smoke test” preset for environment validation.

## Parsing & Interop

- [ ] SBPL parser for round‑trip: `.sb` → `SBPLPolicy` (subset), enabling linting/normalization of existing profiles.
- [ ] Import helpers for Apple sample profiles (best‑effort mapping into our typed subset).

## Docs & Testing

- [ ] Expand design doc with a formal subset spec and explicit non‑goals per macOS version.
- [ ] Golden tests for compiler output; snapshot tests for validation messages.
- [ ] Cookbook examples (policy → effect) and troubleshooting playbook for common denies/aborts.

## Compatibility & Fallbacks

- [ ] Clear behavior when `sandbox-exec` is missing/deprecated (diagnostics, suggested alternatives).
- [ ] Optional translation layer to container/VM policies for dynamic per‑run path scoping (documented outside SBPL core).

---

Notes

- Current implemented subset: `file-read*`, `file-write*`, `file-read-metadata`, `file-map-executable`, `process*`, `signal (target self)`, `network-(inbound|outbound|bind)` with `(local ip)`, `mach-lookup` by global name, `system-socket`, `sysctl-read`, `trace`.
- Keep core layering: models and compiler remain pure; validations/presets/runners are opt‑in and explicit.
