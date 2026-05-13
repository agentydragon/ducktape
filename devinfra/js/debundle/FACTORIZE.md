# Factorize — Design

> Summary: the factorize stage takes the owner graph plus the current
> partition (each owner's destination module under the current spec)
> and proposes a refinement of that partition — a new partition that
> reassigns residual owners into named modules (existing or new) while
> satisfying the materializer's gates. Each currently-named module is
> a **supernode** in the input graph; residual owners are individual
> nodes; the **residual_entry catch-all is not a supernode** because it
> isn't a coherent factor, it's a leftover bucket. The factorize is a
> one-pass graph operation over this supernode graph: build the
> must-co-locate digraph H, take Tarjan SCCs, emit each SCC as a
> proposal. Cells containing an existing supernode propose extending
> that module by the additional residual owners in the cell; cells
> with no supernode propose a new module. There is no separate "grow
> existing" vs "create new" mode — both fall out of the same
> primitive operation.

## Mission

The debundler's job is to recover a multi-module ESM bundle from a
single bundler-emitted chunk, guided by a spec the author writes
incrementally. After the validator accepts a spec, the residual_entry
still holds every owner not yet assigned to a named module. The
factorize asks: **what should the author write next?**

A useful factorize output is a list of proposals, each saying either
"create a new module containing these N bindings" or "extend module M
by these N bindings". Every proposal must be **mechanically valid** —
applying it as a spec edit must result in a spec the materializer
accepts. The author's role is to label and curate the proposals; the
factorize's role is to do the bookkeeping that makes "would this
spec edit pass the materializer?" cheap to answer.

## Domain model

The factorize is a function:

```
factorize(owner_graph, current_partition) -> [Proposal]
```

Inputs:

- `owner_graph` — the same flat-edge owner graph the materializer
  validates against. Nodes are top-level owners; edges are `EagerUse`,
  `LazyUse`, `Sequenced`, `EagerRebind`, `LazyRebind` reasons carrying
  a binding name (except `Sequenced`, which has none).
- `current_partition` — for each owner, its destination module under
  the spec. Each destination is either a named `Logical(M)` module or
  the implicit `ResidualEntry` catch-all.

Outputs: a list of proposals. Each proposal is a set of currently-
residual owners that, together with at most one named module's
existing members, forms a valid module under the materializer's
gates.

### Nodes

Two kinds of node feed the factorize graph:

- **Supernode** = one currently-named logical module. Members: every
  owner currently assigned to that module. The factorize cannot split
  supernodes — the author already committed to grouping these owners.
  The factorize _can_ extend a supernode by absorbing additional
  residual owners (that's what "extend module M" means).
- **Loose node** = one currently-residual owner. The factorize is
  free to assign each loose node to any supernode, to a new module,
  or to leave it in residual.

The residual_entry destination is **not** a supernode. It's a
leftover bucket. Treating it as a supernode would force every loose
node to either join one giant residual_entry supernode (defeating
the point of factorize) or leave it, which is the dichotomy we already
have. The clean model: loose nodes start unassigned (in residual);
the factorize proposes assignments.

### Edges

Edges in the factorize graph come from the owner graph, projected
through "which node does this owner belong to in the factorize graph":

- Owner → owner edge u → v in the owner graph.
- If u and v are both in the same supernode: edge drops (internal).
- Otherwise: factorize-graph edge from node(u) to node(v), carrying
  the same `(kind, binding)` reason.

Multiple owner-level edges between the same factorize-node pair are
kept distinct (we care about each reason's binding individually, not
just the presence of an edge).

## The must-co-locate digraph H

A directed graph on factorize-graph nodes. An edge `a → b` in H means
"if a's module gets b's owners merged in, b must be in that module
too". Closure rules — applied per owner-graph edge, projected to
node-level via the rule above:

- `Sequenced` edge u → v (source-order constraint):
  Adds `node(v) → node(u)` to H. (If v ends up in module M but u
  stays in residual_entry, M materializes before residual_entry,
  so v runs before u — inverting the original source order. To
  keep the order, u must be in M too.)

- `EagerUse` / `LazyUse` edge u → v on binding b:
  - If b is **entry-exported** (in `entry_exported_binding_names` —
    pre-existing entry exports plus any binding already owned by a
    `Logical(_)` module): no H edge. The cross-module read resolves
    through entry, no co-location required.
  - Otherwise (b is declared in residual and not entry-exported):
    `node(u) → node(v)` in H. (If u ends up in module M, M's body
    references b. b is declared by v. If v stays in residual_entry,
    M would need to import b from residual_entry — but b is not in
    entry's exports, so the materializer rejects with emit-block.
    The only fix is to absorb v into M.)

- `EagerRebind` / `LazyRebind` edge u — v:
  Bidirectional `node(u) ↔ node(v)` in H. (LazyRebind gate: declarer
  and assigner of a mutable binding must co-locate.)

Supernode-internal owner-graph edges (both endpoints in one
supernode) don't contribute to H. Supernode-to-loose and
supernode-to-supernode edges follow the same rules as
owner-to-owner — the same reasons, projected to nodes.

## The algorithm

```
1. Build the factorize-graph nodes:
   - For each Logical(M) module: one supernode, members = its owners.
   - For each residual owner: one loose node.
2. Project owner-graph edges to factorize-graph node pairs (skip
   internal-to-supernode edges).
3. Build H per the closure rules.
4. Tarjan-SCC on H. Each SCC = one cell.
5. Each cell becomes one proposal in the output.
```

That's the whole algorithm. Total work: O(|V| + |E|) where V is
factorize-graph nodes and E is owner-graph edges (each consulted
once, projected once).

## Proposal interpretation

Each emitted cell C has:

- A set of supernodes it contains. By construction, a cell can
  contain zero or one supernodes — if it contained two, the materializer
  would already be in a state where those two modules are co-located,
  i.e. the spec is invalid (or one supernode would absorb the other,
  which the partition wouldn't allow). The algorithm's correctness on
  a valid input spec implies at most one supernode per cell. If two
  supernodes do end up in the same SCC, that means the _current_ spec
  is invalid — surface it as a `spec_conflict` proposal so the author
  can investigate.

- A set of loose nodes (currently-residual owners) it contains.

From those, we derive the proposal shape:

- **Cell contains supernode S_M plus K loose nodes (K ≥ 1)**:
  _Extend module M_ by absorbing K loose nodes. The lane worker
  edits `M.yaml` to add the K loose nodes' bindings (or the
  corresponding anonymous-statement selectors for owners with no
  declared binding).
- **Cell contains supernode S_M, no loose nodes**:
  No proposal needed — module M as it stands is already valid. The
  cell exists in the report only to document "here's a supernode the
  algorithm considered; it doesn't need anything added".
- **Cell contains no supernode, just loose nodes**:
  _New module_ containing those loose nodes. The lane worker
  authors a fresh YAML.
- **Cell contains two or more supernodes**:
  _Spec conflict_ — the current spec is invalid in the materializer's
  eyes, but the validator may not have noticed (or the factorize is
  exploring a hypothetical). Surface for human review.

## Validity by construction

Each emitted cell is a valid hypothetical module (per the
materializer's gates) **assuming the cell becomes a module**, with
the following caveats:

- **Emit-resolvability**: by H's closure rules, any non-entry-exported
  use edge between cell members and non-members forces both into the
  same SCC — so an emitted cell has no out-edges to non-cell loose
  nodes on non-entry-exported bindings. Out-edges _to_ other cells via
  entry-exported bindings are fine (materializer routes via entry).

- **LazyRebind**: rebind edges are bidirectional in H, so any rebind
  edge with both endpoints in residual ends up internal to one cell.

- **Sequenced order**: same reasoning as LazyRebind, via the reverse
  edge in H.

- **Cycle gate**: a cell is **landable on its own** iff its module-
  quotient module would have only-outgoing or only-incoming edges
  with residual*entry (so no cycle through residual_entry forms).
  This is verified per cell via the SSOT
  `evaluate_residual_peel_candidate` predicate as a sanity check —
  the closure rules should make this hold by construction, but we
  run the predicate to catch bugs and to populate the cycle blocker
  list when a cell genuinely \_isn't* landable yet (e.g. its closure
  spans into residual that's still tangled).

A cell that's not landable today is still a valid proposal _if_ the
author lands its prerequisites first. The report flags each cell's
`landable_today` accordingly.

## Output shape

Each proposal is a record:

- `target` — discriminated union:
  - `ExtendModule { module_path: String }` (cell contained supernode)
  - `NewModule` (cell of only loose nodes)
  - `SpecConflict { module_paths: [String] }` (cell contained ≥2 supernodes)
- `additional_owners: [OwnerId]` — for `ExtendModule` and `NewModule`,
  the loose nodes the cell adds. Empty for `SpecConflict`.
- `additional_bindings: [BindingName]` — the declared bindings of
  `additional_owners`, deduplicated.
- `landable_today: bool` — verifier verdict.
- `status`, `emit_blocked_residual_bindings`, `cycle_blocker_owner_ids`
  — predicate diagnostics when not landable.
- `size_lines_estimate`, `source_line_range` — size metadata for
  downstream tooling (size-cap heuristics, sorting).

The factorize itself does **not** apply a size cap or merge cells for
aesthetic reasons; those are post-processing concerns the CLI
(`peel_factorize`) handles. The analyzer emits the minimum-forced
cells; the CLI can choose to combine adjacent landable cells under a
size budget, prioritize by source line or shared edges, or honor
spec-author hints. Mixing those heuristics into the analyzer would
mix correctness with policy.

## What this replaces

The current `analysis::factorize` implements only the residual-only
case: it filters out supernode-claimed owners before building H, so
the algorithm can only emit `NewModule` proposals. To propose
extending an existing module, the current code would need a separate
"detection" pass with side-channel signals — exactly the kind of
ad-hoc special-casing this design eliminates.

The closure rules are unchanged; what changes is the node set the
algorithm operates on (all factorize-graph nodes, not just loose
nodes) and the output interpretation (proposal target derived from
the cell's contents).

## Implementation notes

- The factorize-graph nodes are constructed once at the start of
  `build_factorize_report`. Supernodes are identified by their
  `ModuleId::Logical(_)` membership in the schedule's partition.
- Edge projection: for each owner-graph edge, look up the
  factorize-graph node id of each endpoint via an
  `owner_id → node_id` map computed once. Skip edges where both
  endpoints map to the same node id.
- H is a `DiGraphMap<NodeId, ()>` from `petgraph`; SCC via
  `petgraph::algo::tarjan_scc`.
- The verifier predicate
  (`peelability::evaluate_residual_peel_candidate`) takes a candidate
  set of owners. For a cell, the candidate set is the cell's owners
  ∪ (any supernode members it contains) — the materializer sees the
  hypothetical post-application module that way.
- Loose-node SCCs that touch a supernode SCC don't get "absorbed
  into" the supernode in the SCC computation — they live in their
  own SCC node, and the cell-emit step pairs them. (Equivalently, we
  could pre-collapse supernode members into one synthetic node before
  Tarjan; same result, slightly simpler to reason about. The
  implementation should do this pre-collapse.)

## Post-processing (CLI `peel_factorize`)

The CLI consumes the analyzer's proposals from `OwnerGraphReport`
and:

- Annotates each proposal with spec-tree context (`seeded_from_deferred`,
  `active_modules_referenced` from active-claim attribution).
- Optionally agglomerates adjacent landable `NewModule` proposals
  under a `size_cap_lines` budget (current behavior; documented in
  this layer so the analyzer stays heuristic-free).
- May reorder or filter proposals by author-supplied hints.

Heuristics live in the CLI because they're policy, not correctness:
two authors might disagree on whether to combine two unrelated
landable singletons into one module, but they'll agree on what's
mechanically valid.
