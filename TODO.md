# Repository TODOs

## Linting

- [ ] Add a pre-commit linter to enforce the link style convention from STYLE.md: detect `[path](path)` duplicate-path links in markdown and suggest using `@path` transclusion or `<path>` angle bracket syntax instead
- [ ] Add ESLint to pre-commit for local JS/TS linting (currently only runs in CI via Bazel)
- [ ] Consider adding mypy to pre-commit for local type checking (currently only runs in CI via Bazel)

## Dotfiles

- [ ] Merge agentydragon & gpd dotfiles (rcrc)

## System Configuration

- [ ] Add to small laptop installation: nmap, other hacking tools
- [ ] Start Signal minimized (difficult: settings in encrypted sqlite)
- [ ] Consider adding apt-file (heavy dependency)
- [ ] Combine ActivityWatch + HALinuxCompanion to report: session events (login/logout, lock/unlock, suspend/resume), battery charge level, and other device telemetry

## Neovim

- [ ] nvim-treesitter folding setup:

  ```lua
  vim.wo.foldmethod = 'expr'
  vim.wo.foldexpr = 'v:lua.vim.treesitter.foldexpr()'
  ```

## Build System

- [ ] Migrate all Python packages to Bazel monorepo style (colocated tests, flat structure like `git_commit_ai/`)
- [ ] Re-enable `bazel coverage` in CI once compatible with remote execution (RBE). Currently disabled because the Java-based `remote_coverage_tools` can't locate its runfiles on BuildBuddy workers, causing all tests to be marked as failed. See `bazel-test.yml`.
- [ ] Set up BuildBuddy [remote runner features](https://www.buildbuddy.io/docs/remote-runner-features) for artifacts / extra test outputs
- [ ] Upgrade protobuf once UPB uninitialized variable warnings are fixed upstream. Currently on `protobuf 34.0.bcr.1` (latest in BCR as of March 2026). GCC emits `-Wmaybe-uninitialized` warnings from `external/protobuf+/upb/wire/decode.c` (lines 281, 732, 1089: `upb_StringView sv` used uninitialized). These are false positives from GCC's static analysis failing to prove the variable is always set before use. Upstream issues: [#17052](https://github.com/protocolbuffers/protobuf/issues/17052), [PR #18805](https://github.com/protocolbuffers/protobuf/pull/18805). Also `src/google/protobuf/compiler/rust/message.cc` triggers `-Wdeprecated-declarations` for `FieldOptions::weak()`. Monitor protobuf releases >34.0 for fixes.

## Repository

- [ ] Pick a sane license schema (probably AGPL)
