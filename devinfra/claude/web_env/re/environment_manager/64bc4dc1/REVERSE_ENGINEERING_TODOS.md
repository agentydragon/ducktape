# Environment Manager Reverse Engineering - Remaining Work

Binary: `/tmp/env-manager-new` (Build ID: 64bc4dc1, version: release-9f4ec76fbc-ext)

**Status:** Source copied from a6f96673 predecessor, metadata updated to 64bc4dc1.
Build paths corrected. Version string updated. CLI behavior verified.

## Critical Constraint: Binary Is Garble-Obfuscated

The 64bc4dc1 binary was obfuscated using garble (Go obfuscator):

- `go version -m` returns "unknown" — module info stripped
- `go tool nm` returns no output — symbol table garbled
- No DWARF debug info present
- Binary size doubled (27 MB → 49 MB) from inlining and padding
- All function/type names replaced with random identifiers

**DWARF-based reconstruction (as done for a6f96673) is impossible.**

The source in `src/` reflects the a6f96673 binary structure. It is the best
available approximation. All verified-unchanged items (CLI flags, sandbox
settings) are accurate. Internal implementation details cannot be verified.

## What Was Verified (2026-03-26)

- CLI flags: all subcommands (`orchestrator`, `setup`, `task-run`, `poll`,
  `print-sandbox-settings`, `completion`) have identical flags and defaults
- Sandbox settings: `enableWeakerNestedSandbox: false`, same domain lists
- Version string: `release-9f4ec76fbc-ext` (runtime literal, not obfuscated)

## What Cannot Be Determined

Due to garble obfuscation, these items from the a6f96673 RE cannot be verified
or updated for 64bc4dc1:

- Binary addresses in function comments (all stale from a6f96673)
- Dependency versions (go.mod reflects a6f96673 versions)
- New internal functions or packages
- Changes to existing function implementations
- New error messages, log messages not surfaced via CLI

## Future RE Approach (if needed)

To learn more about 64bc4dc1 without DWARF:

1. **Dynamic analysis**: Run the binary with various inputs; observe logs, API
   calls, file system changes
2. **String extraction**: `strings /tmp/env-manager-new | sort -u` — JSON field
   names, error messages, OTEL attributes survive obfuscation
3. **Diff against a6f96673 strings**: `comm -13 <(strings old | sort) <(strings new | sort)`
   reveals new string literals (new features, new error messages)
4. **gdb/Delve under load**: Attach to running process and observe call stacks
   (obfuscated names, but control flow visible)
5. **Network/syscall tracing**: `strace`, `ltrace`, network capture during operation

## Previously Completed (in a6f96673)

All medium and high-priority items were completed for the a6f96673 predecessor.
The 64bc4dc1 source is structurally identical to a6f96673 — only metadata
(build ID, version string, file headers) has been updated.
