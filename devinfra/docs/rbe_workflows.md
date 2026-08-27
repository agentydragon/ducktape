# Why no workflow needs `--remote_executor=""`

`--remote_executor=""` is **never required for correctness** — every workflow in
this repo runs on RBE. The root `AGENTS.md` rule ("RBE is the expected default — do
not disable it") holds for all of these. Full evidence and per-workflow source
analysis: <../../debug/rbe-compatibility/audit.md>.

The mechanics the table cites:

- **Module-extension mechanic**: module extensions (repo rules) always run locally
  on the Bazel client — RBE executes spawn actions, not repository rules — so
  repins and lock updates are local by definition; `--remote_executor=""` changes
  nothing about them.
- **`bazel run` mechanic**: the produced binary always runs locally (that's how it
  writes the source tree); its _build_ still uses RBE.

| Workflow                                           | Why `--remote_executor=""` is NOT needed                                                                                                                                                                                                |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bb run //devinfra:gazelle`                        | `bazel run` mechanic; the Gazelle Go-binary build uses RBE.                                                                                                                                                                             |
| `bb run //devinfra:gazelle_python_manifest.update` | `bazel run` mechanic.                                                                                                                                                                                                                   |
| `//:requirements`                                  | The build action runs on RBE and emits `requirements.out`; download with `--remote_download_regex` and copy it into place.                                                                                                              |
| `CARGO_BAZEL_REPIN=1 bazelisk build @crates//:all` | Module-extension mechanic; the subsequent build actions use RBE.                                                                                                                                                                        |
| `update_pnpm_lock` (`pnpm-lock.yaml`)              | Module-extension mechanic.                                                                                                                                                                                                              |
| Syrupy snapshot updates                            | `BazelAmberExtension` copies `.ambr` files to undeclared outputs on RBE; fetch from `bazel-testlogs/.../test.outputs/` and `cp` back. Skipping that one `cp` is the sole DX shortcut `--remote_executor=""` ever buys — never required. |
| Normal builds/tests                                | RBE is the default and correct choice.                                                                                                                                                                                                  |
