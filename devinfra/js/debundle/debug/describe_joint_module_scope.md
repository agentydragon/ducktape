# `describe` over disjoint module roots

A tree config may assign several disjoint YAML roots to one graph chunk. A
`describe` query over the full modules tree must use the config captured by the
graph and scope its module index, binding homes, source claims, and optional
proposal analysis to every root assigned to that chunk.

The regression fixture uses neutral roots `left`, `right`, and `other`: the
first two share a chunk, while `other` belongs to a different chunk and
contains a colliding module leaf and binding name. Run `describe` with
`--tree-config <config> --modules <full modules tree>`. Module path lookups,
including a unique bare filename, are resolved within the selected chunk's
roots. Claims from those roots go through one shared selector-resolution pass;
the unrelated root does not add binding homes or participate in query
interpretation. `--include-proposals` receives the same config.

Report paths use different coordinate systems: source-unit IDs are local to the
graph chunk, while described module paths and binding-home paths retain their
authored-root prefix. Existing proposal rows can use paths relative to each
authored root; do not treat those row paths as source-unit or describe-query
identifiers.

Before this query scope existed, `describe` with `--modules` set to the full
modules tree failed graph compatibility with `ModuleScope`; `describe` did not
accept `--tree-config`. Removing that check alone would have let unrelated
YAML contaminate binding-home and claim resolution. The e2e fixture exercises
the base failure and verifies the scoped behavior, including rejection of a
different config path and changed source bytes.

The scoped module index and tree config are related resolver inputs; passing
each as a separate argument made the source-unit resolver exceed the project's
Clippy argument limit. Keep these inputs grouped in its context. The module
path lookup returns `Option`, so use `?` when there is no path candidate.

`inspect-source --id unit:<id>` uses a saved graph and explicit source root;
it does not interpret module YAML. Keep ID dispatch and source-selection file
loading lazy so this query does not require an existing modules directory.

The regression fixture invokes `debundle --spec-root <fixture-root>
inspect-source --id unit:0 --graph <saved-graph> --modules
<fixture-root>/missing-modules --source-root <fixture-root>/snapshot --view raw
--format json`. Before the lazy lookup fix, `top_level_query_cli_test` failed
with an IO error reading `missing-modules` (Bazel invocation
`087da76b-ac97-409b-a458-32af318b1905`, exit 3). The same test passed after the
fix and returned the selected statement (`7b3e56fe-a10e-4ad4-81fc-90b59a9062b2`,
exit 0).

Final focused validation ran the `:peel`, `:peel_test`, `:cli`, `:cli_test`
targets and the top-level query, chunk-scoped describe, colon-module, and graph
provenance e2e tests. Their Clippy aspects and six tests passed in invocation
`f79ff77b-1f3e-4236-9a9e-ffaba4652b48` (exit 0).
