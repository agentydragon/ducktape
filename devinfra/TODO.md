# TODO

## Orphaned test files

- [ ] Consider detecting orphaned (unwired) test files mechanically: a `test_*.py` or
      `*_test.py` that no `BUILD` file names never runs in CI, and nothing flags it.
      `//devinfra/orphans:find_orphans_bin` already lists git-tracked files that no Bazel
      target names, but no workflow runs it with `--check`. Open question: whether to
      gate on that, on a narrower test-file-only check, or not at all.

## `@pytest.mark.asyncio` ban

- [ ] Consider enforcing the existing `@pytest.mark.asyncio` ban (pytest-asyncio auto mode is on
      repo-wide) with a ruff rule. The owner prefers agent-instruction text over new lint code,
      so the existing `AGENTS.md` rule may suffice.
