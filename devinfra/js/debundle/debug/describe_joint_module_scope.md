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
