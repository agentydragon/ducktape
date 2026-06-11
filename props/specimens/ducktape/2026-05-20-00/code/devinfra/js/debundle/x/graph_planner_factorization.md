# Graph Planner And Factorization

This note is the scratch design space for graph-derived module
planning in the debundler. It is intentionally corpus-neutral: keep
private downstream findings in the downstream repo, and move only the
reusable command contracts, graph vocabulary, and algorithm backlog
here.

## Current Surface

The production planner surface is the `debundle peel plan-work`
subcommand:

```bash
debundle peel plan-work \
  --graph <owner_graph.json> \
  --modules <spec-root>/modules \
  --size-cap-lines 10000 \
  --limit 25
```

Related read-only queries:

- `debundle peel candidates`
- `debundle peel patch-status`
- `debundle peel explain`
- `debundle peel source-slice`

`plan-work` is the ordered dispatch surface for authoring work. It
reads the owner graph and the current spec module tree, emits bounded
proposal records, and leaves final names and paths to human or agent
review. Older per-symbol queues and corpus-specific scratch notes are
orientation only; the live graph decides what is actually peelable.

## Graph Vocabulary

The owner graph `G = (V, E)` has top-level statement owners as
vertices. Edges are typed by whether they constrain ESM materialization:

- At-init reads constrain realizability.
- Side-effect sequencing constrains realizability.
- Cross-module rebinding is forbidden because imported ESM bindings are
  read-only.
- Lazy reads do not constrain module initialization order, though they
  can still matter for emit-side import/export surfaces.

A module assignment `f : V -> M` is realizable when the quotient graph
`G/f`, restricted to realizability-constraining edges, is a DAG. A peel
proposal is landable when moving its owner set into a module preserves
that quotient DAG and every referenced external binding can be emitted
as an import from an exporting module.

Strongly connected components of the constraining-edge graph are
atomic for module splitting unless the analysis can refine the edge
kind. The condensation graph is therefore the natural substrate for
planner proposals.

## What The Current Planner Covers

The current planner already covers the minimum useful loop:

1. Read `owner_graph.json`.
2. Respect the live spec module tree.
3. Emit graph-certified proposal records.
4. Keep output bounded and deterministic enough for agent use.
5. Provide follow-up inspection commands for source, graph, and patch
   status.

That is enough to replace repeated candidate rediscovery. The remaining
work is about proposal quality, module shape, and making the planner
more explicit about why one proposal is better than another.

## Desired Next Shape

The next planner design should extend the current command rather than
add a parallel CLI:

1. Keep `debundle peel plan-work` as the public command.
2. Add proposal metadata instead of inventing a second output contract.
3. Preserve bounded JSON output for agents.
4. Make algorithm choices explicit options only after at least two
   algorithms are real and tested.
5. Treat corpus taxonomy as consumer policy, not debundler core logic.

Useful proposal metadata would include source-locality scores,
cross-cell edge summaries, likely naming anchors, size buckets, and
warnings when a proposal is graph-valid but likely awkward for humans.

## Algorithm Backlog

### SCC Condensation And Bin Packing

Collapse SCCs of constraining edges, then greedily merge singleton
cells by source proximity and low cross-cell edge weight.

Good: deterministic, simple, respects realizability.
Risk: high-degree hubs can still dominate and produce huge cells.

### Hierarchical Clustering

Cluster owners by graph distance plus a source-line locality penalty,
then cut the dendrogram into bounded module-size cells.

Good: can expose multiple possible granularities.
Risk: `O(N^2 log N)` can get tight, and quality depends heavily on the
distance metric.

### Modularity Maximization

Use Louvain or Leiden-style community detection, then split oversize
communities and merge undersize ones.

Good: fast and standard for graph communities.
Risk: resolution limits can merge small conceptual modules.

### Balanced Graph Partitioning

Use METIS, hMETIS, or KaHyPar-style partitioning with explicit size
targets.

Good: directly optimizes balanced partitions.
Risk: requires picking the number of partitions and may cut through
human-local source regions without extra scoring.

### Structural Decomposition

Compute articulation points, bridges, biconnected components, k-cores,
and dominator-like hierarchies on the condensation graph.

Good: cheap, explainable, and useful for boundaries.
Risk: structural cuts are often too literal and need scoring before
they become authoring proposals.

### Hub-Aware Postprocessing

Identify high-degree owners and either postpone them, score them
separately, or split by call shape if the owner model is fine-grained
enough.

Good: attacks a common pattern in minified application bundles.
Risk: arbitrary special cases unless the rule is grounded in graph
metrics.

## Analysis Backlog

These are generic blocker patterns observed in real bundles and worth
representing as debundler-side analysis work when they recur:

- **Per-declarator owner splitting.** Multi-binding declarations can glue an
  impure initializer to unrelated pure bindings. The owner model should split
  declarators when doing so preserves emitted semantics.
- **Class plus decorator co-attribution.** Transpiled decorator application
  blocks often belong atomically with the class they decorate. Treating the
  decorator call as an unrelated side-effect owner can make valid class peels
  look over-broad.
- **Mutable binding co-move diagnostics.** When a class or helper mutates a
  `let` binding, the writer and binding owner must move together or stay
  together. The planner should surface that as an explicit co-move reason, not
  as a vague blocked closure.
- **Large function-internal seams.** A single giant handler can contain
  independent semantic branches that are invisible to owner-level splitting.
  That needs a separate finer-grained model; planner output should identify
  "blocked by function-internal granularity" instead of proposing arbitrary
  owner moves.

## Agent Layer

The practical agent architecture is:

1. Run `debundle peel plan-work`.
2. Select a bounded set of proposals.
3. Have humans or agents assign final names and paths.
4. Reject bad-shape proposals before YAML edits.
5. Dispatch workers on disjoint owner or binding sets.

A later per-partition naming layer can sit on top of the planner:

- one agent per selected proposal, not one per binding
- each agent reads the proposal source slice and graph neighbors
- output is name, path, and rationale
- direct spec edits are allowed only when the ownership set is scoped
  and non-overlapping

The planner should do graph reasoning. Agents should do naming and
shape judgment.

## Prototype Options

For one-off experiments against `owner_graph.json`, Python with
NetworkX, igraph, or a partitioner binding is acceptable. For a
shipping debundler mode, prefer Rust plus existing graph/data
structures first, and add external partitioners only after the option
surface and reproducibility story are clear.

At current bundle-graph scales, performance is rarely the hard part.
The hard part is the objective: edge-cut minimization, balanced sizes,
modularity, source-line locality, layer-respecting taxonomy, or some
weighted combination.
