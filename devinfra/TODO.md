# TODO

## TypeScript build refactor

- [ ] Compare options in bounded spikes before choosing a build layout or generator. See the
      [code reduction decision](../docs/trimcode_decisions.md#typescript-build-refactor).

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

## Pydantic definition registration (schema audit C)

Register the supplemental CSV request and WebSocket frame schemas in
`finance/augur/api/export_schema.py` and `study_casino/export_schema.py` without
silently keeping or replacing incompatible names in FastAPI's component registry.
Preserve each owner's schema mode. Verify that emitted references resolve, conflicts
fail clearly, and frontend typechecks and representative wire payloads pass.

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
