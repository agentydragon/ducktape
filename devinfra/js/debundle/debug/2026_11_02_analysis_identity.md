# Analysis executable and compiled identity

The Analyze action previously depended on the entire CLI executable, and its
artifact header hashed that executable. CLI frontend edits therefore invalidated
the expensive analysis action. A separate analyzer executable alone does not
solve reader compatibility: final linking can discard decoder code. Packaging
can also rewrite ELF executable bytes.

The shared boundary is the configured compiled `pipeline` closure. Its generated
identity includes the pipeline object, transitive Rust objects, compile/runtime
data, build-script outputs and flags, selected native archives and `alwayslink`,
ordered native link flags, and Rust/C++ toolchain files and link configuration.
The current dependency provider reports 296 Rust crates and 40 build-info entries.
The analyzer and CLI embed the same generated identity without introducing a
dependency from `pipeline` back to that generated crate. Saved graph provenance
continues to hash the actual full CLI executable.

## Checks

All commands used the Nix devshell and ordinary Bazel RBE/cache configuration.
The main contract invocation
[bba22be4](https://app.buildbuddy.io/invocation/bba22be4-752a-4141-b8f8-efec176526cc)
passed all ten test targets: staged analysis, exact saved-graph provenance, and
eight action tests. The same invocation directly built/linted the new Python
library and materialized the analyzer, CLI and generated identity outputs.

The staged test produces artifacts through the dedicated analyzer and through a
standalone copy of the CLI with an empty environment, compares their bytes, and
loads the result through the standalone CLI. It also changes the identity or
parser-input digest in a real successful artifact while retaining its valid
body, and checks refusal. Existing projection mismatch coverage remains.
Action tests require the Analyze tool to exclude the full CLI; other pipeline
actions retain it. The identity action must include pipeline, lowering, codec
and fact-analysis compiled objects and exclude the CLI object.

Separate temporary source probes used:

```sh
nix develop --command bazelisk build \
  //devinfra/js/debundle:analysis_identity_source \
  //devinfra/js/debundle:debundle_analyze \
  --remote_download_outputs=toplevel --noannounce_rc
```

The CLI probe also built `:debundle_e2e`. SHA-256 prefixes below describe the
actual generated identity and executable bytes, rather than inferred source
dependencies.

| Probe                          | Compiled engine ID | Analyzer executable | Full CLI executable |
| ------------------------------ | ------------------ | ------------------- | ------------------- |
| Baseline                       | `594e5bfe8480c79e` | `6b4ee7cc09acbebb`  | `e3aac52ef0b6b64a`  |
| CLI help text only             | `594e5bfe8480c79e` | `6b4ee7cc09acbebb`  | `2b13ce43ad832f27`  |
| Decoder error context only     | `5ebb36a343aa5f68` | `c758261d06fffa33`  | Not rebuilt         |
| Conservative for-of await fact | `2af8c096fb8f6fbe` | `bfb14edfcae3c876`  | Not rebuilt         |
| Sources restored               | `594e5bfe8480c79e` | `6b4ee7cc09acbebb`  | `e3aac52ef0b6b64a`  |

The CLI-only build
[f786cc78](https://app.buildbuddy.io/invocation/f786cc78-8226-456b-b4d2-0332d40bf25b)
performed CLI compilation/linking while reusing the unchanged analyzer and
identity outputs. The decoder-only change touched `analysis_codec::decode`,
leaving the writer unchanged
([6c18e8e1](https://app.buildbuddy.io/invocation/6c18e8e1-884e-4ddc-a9c2-af7211105185)).
The fact probe made `TopLevelAwaitFinder::visit_for_of_stmt` conservatively record
an await for every for-of loop
([a6c3a6df](https://app.buildbuddy.io/invocation/a6c3a6df-44b0-47c8-9172-b39b03953ba0)).
Each probe was restored from an exact saved source copy.
The restored final invocation
[f978b13d](https://app.buildbuddy.io/invocation/f978b13d-ba90-449f-9467-4eda916419cf)
passed all ten targets from cache and restored all three baseline digests.

## Boundaries and rough edges

- The current `pipeline`/`lowering` closure also contains emission code and a
  substantial native solver closure. Those edits conservatively invalidate the
  identity. Only edits confined to `cli/` have the demonstrated isolation, and
  Analyze reuse still requires unchanged projection bytes and source inputs.
- The collector mirrors pinned rules_rust 0.74 input/linking policy and reuses
  its PIC/library-selection helpers. Re-audit it when upgrading those rules.
  Native linkstamps, object-only native libraries, experimental C++ linking,
  Rust linker preference and directory symlinks inside tree inputs fail closed.
- Custom tool pairs must share the compiled closure and native linking policy.
  Binary-specific linking overrides and external system libraries are outside
  this compiled-closure identity. The dedicated analyzer accepts analysis options
  directly, as documented in `docs/bazel_integration.md`.
- Hashing toolchain `all_files` is conservative cold work. This check establishes
  invalidation behavior, rather than a latency improvement measurement.
- Changing Rust inputs during an active remote upload caused a digest mismatch
  in an earlier invocation. Freeze compilation inputs until the command exits;
  the restored final check is the authoritative validation result.
