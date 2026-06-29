# Why no workflow needs `--remote_executor=""`

`--remote_executor=""` is **never required for correctness** — every workflow in
this repo runs on RBE. The root `AGENTS.md` rule ("RBE is the expected default — do
not disable it") holds for all of these. Full evidence and per-workflow source
analysis: <../../debug/rbe-compatibility/audit.md>.

| Workflow | Why `--remote_executor=""` is NOT needed |
| --- | --- |
| `bb run //devinfra:gazelle` | `bazel run` always executes the binary locally; the Gazelle Go-binary build uses RBE. |
| `bb run //devinfra:gazelle_python_manifest.update` | Same — binary runs locally and writes the source tree. |
| `//:requirements` | The build action runs on RBE and emits `requirements.out`; download with `--remote_download_regex` and copy it into place. |
| `CARGO_BAZEL_REPIN=1 bazelisk build @crates//:all` | The repin is a **module extension** — module extensions always run locally on the Bazel client, never on RBE. The subsequent build actions use RBE. |
| `update_pnpm_lock` (`pnpm-lock.yaml`) | Also a module extension — runs locally regardless. |
| Syrupy snapshot updates | `BazelAmberExtension` copies `.ambr` files to undeclared outputs on RBE; fetch from `bazel-testlogs/.../test.outputs/` and `cp` back. `--remote_executor=""` only saves that one `cp` — a DX shortcut, never required. |
| Normal builds/tests | RBE is the default and correct choice. |

Two mechanics make this hold:

- **Module extensions (repo rules) always run locally on the Bazel client.** RBE
  executes spawn actions, not repository rules — so repins and lock updates are
  local by definition; `--remote_executor=""` changes nothing about them.
- **`bazel run` always runs the produced binary locally** (that's how it writes the
  source tree). Its *build* still uses RBE.

So the only place `--remote_executor=""` ever appears is the optional syrupy DX
shortcut, and even there the RBE path (`BazelAmberExtension` + undeclared outputs)
is fully functional.
