# Python LSP (Pyright) in This Monorepo

## Investigation status

Historical observations from the 2026-05 investigation, not a fresh LSP check:
`pyright-lsp@claude-plugins-official` was enabled through
[nix/home/claude_code/default.nix](../../nix/home/claude_code/default.nix), using
Nix's Pyright 1.1.407 and Python 3.13.12 environment.

- Working: documentSymbol; hover for stdlib/Nix-installed packages;
  goToDefinition for stdlib, pydantic and local imports; stdlib findReferences.
- Not working: workspaceSymbol (empty), non-stdlib findReferences, and definitions
  or type information for Bazel-only dependencies absent from the interpreter's
  `sys.path` (examples included `fastmcp`, `mcp` and `openai`).

No root `pyrightconfig.json` currently supplies `bazel-*` exclusions,
`pythonVersion` or Bazel `extraPaths`. Bazel wheel paths are machine-specific:
`<output_base>/external/rules_python++pip+ducktape_pypi_314_<name>/site-packages/`.
The investigation found no Pyright equivalent of Rust's `discoverConfig`; any
Bazel-path generation would need an external trigger.

Claude Code's shared plugin/configuration restrictions are recorded in
[Rust LSP notes](../rust/LSP_NOTES.md#claude-code-lsp-limitations-as-of-2026-05).
Pyright used its native `pyrightconfig.json` fallback when client configuration
was unavailable; its server also started lazily on matching file access.

## Unselected integration options

| Approach               | Mechanism                                                                                                                              | Remaining cost                                                              |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Generated `extraPaths` | Extract `PyInfo.imports` with `bazel cquery //... --output starlark`, map imports to external wheel paths, write `pyrightconfig.json`. | Machine-specific, gitignored config; needs regeneration.                    |
| Synthetic venv         | Generate `.venv/lib/python3.14/site-packages/_bazel_deps.pth` listing wheel directories for venv discovery.                            | Machine-specific paths, gitignored venv; needs regeneration and validation. |
| `pythonPath` wrapper   | Inject Bazel wheel paths into the interpreter's `sys.path`.                                                                            | Maintain/generate the wrapper and validate discovery.                       |

The existing research candidate
[agoessling/rules_pyright](https://github.com/agoessling/rules_pyright) has a
`pyright_aspect` and a `tools/update_pyrightconfig.py` implementing the cquery
approach. The investigated local source is
`~/code/rules_pyright/tools/update_pyrightconfig.py`; this is not a new upstream
compatibility assessment.

The generation trigger remains undecided:

- Manual: simplest, but requires an agent/editor action.
- Claude session-start hook: automatic for Claude only; cquery adds startup latency.
- direnv: broader editor coverage on directory entry, with the same latency concern.
- Bazel-built venv: explicit `bb run`, repeated when dependencies change.

## TODO

- [ ] Decide on the trigger and implement the generation script.
- [ ] Create `pyrightconfig.json` with exclusions and `extraPaths`.
- [ ] Test all LSP operations with Bazel-only packages.
- [ ] Add a Claude Code command for LSP testing.
- [ ] Test via `z-claude --print`.
