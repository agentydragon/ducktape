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

## Pydantic definition registration (schema audit C)

Standardize definition allocation and collision checks in `agentplane/app/export_schema.py`,
`finance/augur/api/export_schema.py`, `study_casino/export_schema.py`, and
`airlock/export_schema.py`. Evaluate Pydantic's multi-type `TypeAdapter.json_schemas` before adding
another merger. Preserve each owner's validation/serialization mode and stream/WebSocket/CSV
models; reject incompatible duplicate names instead of silently keeping or replacing one schema.
Done: emitted refs resolve, conflicting-name fixtures fail clearly, and existing consumer typechecks
and representative wire payloads pass. No collision in current production models was demonstrated.

## Lightweight OpenAPI extraction (schema audit E)

Separate route/model registration from acquiring runtime resources in the Agentplane, Augur, and
Haku app factories. Their exporters currently construct placeholder clients or materialize temporary
configuration/catalogs just to call `app.openapi()`. Keep the actual production route graph as the
source; do not add a parallel schema-only app or a universal fake-dependency exporter.
Done: each exporter uses the real routes without constructing unrelated runtime resources, and its
operation/model contracts and frontend consumers remain unchanged.

## Generated Python import indexing (schema audit F)

Derive Gazelle import-to-target mappings from `cdk8s_import` declarations instead of maintaining a
second module roster in `cluster/cdk8s/BUILD.bazel`, provider BUILD files, and Agentplane BUILD files.
Preserve package scope; repeated aliases are not automatically redundant. Done: generated bindings
resolve from all current consumers, Gazelle converges, and adding an importer requires no second
hand-maintained mapping. This belongs in generated-module indexing, not CRD schema conversion.
