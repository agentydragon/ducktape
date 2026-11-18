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

## Generated Python import indexing (schema audit F)

Derive Gazelle import-to-target mappings from `cdk8s_import` declarations instead of maintaining a
second module roster in `cluster/cdk8s/BUILD.bazel`, provider BUILD files, and Agentplane BUILD files.
Preserve package scope; repeated aliases are not automatically redundant. Done: generated bindings
resolve from all current consumers, Gazelle converges, and adding an importer requires no second
hand-maintained mapping. This belongs in generated-module indexing, not CRD schema conversion.

## Canonicalize Bazel image pin usage

- [ ] Audit how Bazel image pins and OCI targets are represented and consumed across the repo;
      determine whether one convention can keep each target paired with its authoritative digest,
      including BuildBuddy recycling keys and other compatibility checks. Consider locally built
      images and Bazel's analysis/execution boundary. Evaluate shared key names, canonical ordering
      and compact hashes, including the cost of invalidating existing runner families.
