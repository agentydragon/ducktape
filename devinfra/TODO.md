# TODO

## `@pytest.mark.asyncio` ban

- [ ] Consider enforcing the existing `@pytest.mark.asyncio` ban (pytest-asyncio auto mode is on
      repo-wide) with a ruff rule. The owner prefers agent-instruction text over new lint code,
      so the existing `AGENTS.md` rule may suffice.

## Pydantic definition registration (schema audit C)

Decide how Study Casino should represent its WebSocket invalidation signal. Its
frontend currently consumes a supplemental schema from `study_casino/export_schema.py`.
If retained, check schema mode, nested references and name conflicts. Verify frontend
typechecking, cross-tab refresh, and malformed-frame handling.

## Generated Python import indexing (schema audit F)

Derive Gazelle import-to-target mappings from `cdk8s_import` declarations. The Python
resolver indexes literal `srcs`, and the macro sets those only during Bazel evaluation.
The cdk8s and Agentplane package trees still need explicit mappings for generated modules.
Reuse inherited directives and preserve package scope when adding new mappings.

Completion requires Gazelle to converge for all current consumers. A new binding
should need only its `cdk8s_import` declaration, with no separate consumer mapping.

## Canonicalize Bazel image pin usage

- [ ] Audit how Bazel image pins and OCI targets are represented and consumed across the repo;
      determine whether one convention can keep each target paired with its authoritative digest,
      including BuildBuddy recycling keys and other compatibility checks. Consider locally built
      images and Bazel's analysis/execution boundary. Evaluate shared key names, canonical ordering
      and compact hashes, including the cost of invalidating existing runner families.
