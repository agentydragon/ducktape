# TODO

## Orphaned test files

- [ ] Consider detecting orphaned (unwired) test files mechanically: a `test_*.py` or
      `*_test.py` that no `BUILD` file names never runs in CI, and nothing flags it.
      `//devinfra/orphans:find_orphans_bin` already lists git-tracked files that no Bazel
      target names, but no workflow runs it with `--check`. Open question: whether to
      gate on that, on a narrower test-file-only check, or not at all.
