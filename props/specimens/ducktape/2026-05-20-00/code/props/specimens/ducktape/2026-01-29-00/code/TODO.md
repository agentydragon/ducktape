# Repository TODOs

## Linting

- [ ] Add a pre-commit linter to enforce the link style convention from STYLE.md: detect `[path](path)` duplicate-path links in markdown and suggest using `@path` transclusion or `<path>` angle bracket syntax instead
- [ ] Decide what to do with `trivial-patterns` (adgn linter) - add to pre-commit or remove
- [ ] Create unified linter script (`//tools/lint`) like `//tools/format` to run ruff/buildifier via Bazel, eliminating version drift between pre-commit hooks and Bazel aspects
- [ ] Add ESLint to pre-commit for local JS/TS linting (currently only runs in CI via Bazel)
- [ ] Consider adding mypy to pre-commit for local type checking (currently only runs in CI via Bazel)

## Dotfiles

- [ ] Merge agentydragon & gpd dotfiles (rcrc)
- [ ] Use rcm's `symlink_dirs` feature

## System Configuration

- [ ] Add to small laptop installation: nmap, other hacking tools
- [ ] Start Signal minimized (difficult: settings in encrypted sqlite)
- [ ] Consider adding apt-file (heavy dependency)
- [ ] Get rid of login_event_webhook_reporter (have activitywatch; might combine with halinuxcompanion)

## Neovim

- [ ] nvim-treesitter folding setup:

  ```lua
  vim.wo.foldmethod = 'expr'
  vim.wo.foldexpr = 'v:lua.vim.treesitter.foldexpr()'
  ```

## Build System

- [ ] Migrate all Python packages to Bazel monorepo style (colocated tests, flat structure like `git_commit_ai/`)

## Testing

- [ ] Add automated check for missing `pytest_bazel.main()` in py_test targets (validation test using `bazel query` + AST parsing, or pre-commit hook for new test files)

## Repository

- [ ] Pick a sane license schema (probably AGPL)
