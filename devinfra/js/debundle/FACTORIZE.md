# Factorize — Design

> Summary: the factorize stage takes the owner graph + the author's
> YAMLs + a chunk-level `unassigned_mode` setting, and produces (1)
> **the complete partition** of owners into output factors and (2)
> a list of **advisory proposals** for the author to consider when
> writing more YAMLs. The materializer consumes the partition and
> emits one `.js` per factor. There is no special "residual" code
> path: the residual catchall is just a factor like any other, whose
> destination happens to be `residual_entry`. Mode (a) (`catchall`)
> collects all unassigned owners into one residual factor; mode (b)
> (`mini_factors`) emits each unassigned atomic unit as its own
> synthetic factor. Both modes share the same Stage-1 structural
> work (Tarjan-SCC of the constraining-edge graph → atomic factor
> units); the mode picks how to assemble Stage-2 factors out of
> those units.

## Mission

The debundler recovers a multi-module ESM bundle from a single
bundler-emitted chunk, guided by a spec the author writes
incrementally. The pipeline currently has two coupled jobs:

- **Compute a partition** of every top-level owner into a destination
  module. Some destinations are author-named (logical modules from
  YAMLs); the rest is a leftover bucket.
- **Materialize** the partition into `.js` files with explicit
  imports/exports.

Today those jobs are tangled: the materializer has explicit logic
for the leftover bucket (`residual_entry`), the validator treats
residual differently from logical modules, peelability has its own
"is this destination residual?" branches. The factorize stage —
which decides what _could_ go where — runs alongside but in a
parallel universe, only operating on the residual subset.

The reason to clean this up: every pipeline stage that says
`if module is residual: …` is a place where the model could be
simpler. Once a partition is fixed, every output module is the same
shape from the materializer's point of view. The leftover-ness of
`residual_entry` is an attribute of how it was _populated_, not how
it gets _emitted_.

## Architecture

```
owner_graph + spec (YAMLs) + chunk-config
            ↓
        FACTORIZE
            ↓
   ┌────────┴─────────┐
   ↓                  ↓
partition         proposals
   ↓                  ↓
MATERIALIZE      (advisory: author reads, edits YAMLs)
   ↓
.js files
```

Factorize emits two products. The partition is **authoritative** —
the materializer trusts it to be complete and valid. The proposals
are **advisory** — they're greedy recommendations for what the author
could put in the next YAML; nothing in the pipeline reads them as
input.

## Stage 1 — Atomic factor units

**Mode-independent.** Computed once per chunk regardless of how the
factorize will assemble factors downstream.

The chunk's owner graph has edges with kinds: `EagerUse`, `LazyUse`,
`EagerRebind`, `LazyRebind`, `Sequenced`. Construct a directed graph
`G_atomic` on all owners with edges:

- `EagerUse` u → v: add `u → v` in `G_atomic`. (u depends on v at
  init time. If they form a cycle, they're not separable.)
- `LazyUse`: skipped. Lazy reads happen at call time, not init time,
  so they don't constrain co-location.
- `EagerRebind` / `LazyRebind` u — v: add **both** `u → v` and
  `v → u`. (LazyRebind gate: declarer and assigner of a mutable
  binding must materialize in one destination.)
- `Sequenced` u → v: add `u → v` **and** `v → u`. (Source-order
  side-effect constraint runs both ways: if v moves and u doesn't,
  source order inverts; if u moves and v doesn't, same. Both
  directions force co-location.)

Tarjan-SCC `G_atomic`. Each SCC is an **atomic unit**. Inter-unit
edges form a DAG by Tarjan's construction. **Any valid factorization
of this chunk must keep each atomic unit's members together** — this
is purely structural and depends only on the chunk's init order, not
on the spec.

The atomic units are the primitive nodes of every later stage. From
here on, "node" means "atomic unit" unless explicitly stated
otherwise.

## Stage 2 — Assigning units to factors

Stage 2 turns the DAG of atomic units into the final partition. It
has three inputs:

- The atomic-unit DAG (from Stage 1).
- The author's YAMLs, which assign some owners (transitively, some
  atomic units) to named logical modules.
- The chunk's `unassigned_mode` setting.

### Pre-assigned units (from YAMLs)

Each YAML module `M` has `members: [bindings/anonymous-statement
selectors]`. The resolver maps each member to its declaring owner,
then to its atomic unit. After resolution, each YAML maps to a set
of atomic units it claims.

Two YAML modules may not claim overlapping atomic units. If they do,
the spec is inconsistent — the factorize emits a `SpecConflict`
proposal and the validator separately rejects the spec.

After applying all YAMLs, the atomic units split into:

- **Claimed units**: every unit referenced by some YAML.
- **Unclaimed units**: everything else.

### Mode-specific assembly of unclaimed units

`unassigned_mode` is a per-chunk setting:

- `unassigned_mode: catchall` (default) — one factor named
  `residual_entry` absorbs every unclaimed atomic unit.
- `unassigned_mode: mini_factors` — every unclaimed atomic unit
  becomes its own factor, with a synthetic destination name (see
  _Naming_).

Both modes produce a complete partition: every atomic unit
(and therefore every owner) ends up in exactly one factor.

#### catchall

```
factor R = { destination: "residual_entry", members: <every unclaimed atomic unit> }
factors = [factor per YAML] ∪ {R}
```

The residual factor `R` may be enormous and may itself fail the
materializer's gates if applied as a single module — but historically
the materializer treats `residual_entry` specially (it's allowed to
absorb anything because the resulting `R` carries the bundle's
init-time tangle internally). Under the new architecture this is no
longer "special"; the materializer just emits `R` like any other
factor. The cycle-gate property comes for free because `R` contains
all atomic units that don't have homes — any cycle they participate
in is internal to `R`.

#### mini_factors

```
factor F_u = { destination: <synthetic-name(u)>, members: {u} } for each unclaimed atomic unit u
factors = [factor per YAML] ∪ {F_u for each unclaimed u}
```

Stage 1 already guarantees the inter-unit graph is a DAG, so the
inter-factor module quotient is a DAG too. Validity is automatic.

The result is many small `.js` files instead of one big residual file
— a more granular representation of the part of the chunk the author
hasn't claimed yet.

### Naming synthetic factors (mode b)

Synthetic factors need stable, meaningful file paths. Default rule:

- If the atomic unit has a declared binding, use the lexicographically
  smallest binding name: `synthetic/<binding>.js`.
- If the atomic unit is anonymous side-effect only, use the source-
  line number of its first owner: `synthetic/line_<N>.js`.

Authors can override by adding a YAML that claims the atomic unit
under a meaningful name — at which point it stops being synthetic
and becomes a regular logical module.

## Advisory proposals

In addition to the partition, factorize emits a list of advisory
proposals: greedy recommendations for what the author could write
next. These are computed from the atomic-unit DAG + the current
claim state; the partition itself doesn't depend on them, and
nothing auto-applies them.

The closure-graph algorithm for proposals operates over a graph
where:

- Each currently-claimed YAML module is a supernode (members =
  the atomic units it claims).
- Each currently-unclaimed atomic unit is a loose node.

Closure rules build a "must-co-locate-to-be-a-valid-proposal"
digraph `H` on this factorize graph:

- `EagerUse` or `LazyUse` u → v on binding b:
  - If b is entry-exported under the current claim state (auto-
    exported by some YAML, or in `pre_existing_entry_exports`): no
    `H` edge. The materializer can resolve the import through
    entry; co-location isn't required.
  - Otherwise (b is currently in the residual catchall and not entry-
    exported): `H` edge `u → v`. This is the **mode-(a)-specific
    emit-block force**: in mode (b) every binding is in some real
    module that exports it, so emit-block can't occur. In mode (a),
    proposing to peel u out without v would create an unresolvable
    import.
- `EagerRebind` / `LazyRebind`: bidirectional in `H`. (LazyRebind
  gate.)
- `Sequenced`: bidirectional in `H`. (Source-order constraint.)

Tarjan-SCC on `H` gives **proposal cells**. Each cell's contents
decode to a proposal:

- Cell contains supernode `S_M` plus `N ≥ 1` loose nodes:
  `ExtendModule { module: M, additional: <N owners> }`. The author
  could edit `M.yaml` to claim those additional owners.
- Cell contains no supernode, just `N ≥ 1` loose nodes:
  `NewModule { suggested_members: <N owners> }`. The author could
  write a new YAML.
- Cell contains supernode `S_M`, no loose nodes: no proposal
  emitted (the module is stable; nothing to add).
- Cell contains ≥ 2 supernodes: `SpecConflict { modules: [...] }`.
  The current spec is inconsistent (already true regardless of
  proposals; surfaced here for visibility).

Each proposal carries `landable_today` (whether applying just this
proposal would yield a valid spec on its own) and the gate
diagnostics from `evaluate_residual_peel_candidate` for cases when
it isn't landable. Mode (b) proposals are almost always landable
because every loose node already lives in its own factor.

The proposals are **a function of the current claim state**, so
they update naturally as the author edits YAMLs: claim some
unclaimed owners → those owners become part of a supernode → the
next factorize emits different proposals.

## What the materializer no longer needs to know

Once Stage 2 emits factors uniformly, the materializer's
`materialize_logical_modules` stops branching on whether a module is
residual:

- The `ModuleReportRef.residual: bool` field becomes a display hint
  (so consumers can label the catchall in UI) rather than a control-
  flow signal.
- `validate_schedule` checks every factor under the same rules; no
  separate path for `ResidualEntry`.
- `peel_emit_blocked_residual_bindings` becomes
  `peel_emit_blocked_bindings` over any candidate cell — it doesn't
  need to know which destination is "residual".
- The `include_residual: bool` option on the fixture / pipeline
  config goes away; mode (a) vs mode (b) replaces it.

The `ModuleId::ResidualEntry` enum variant is the only thing that
stays distinguishable, because it identifies a specific path
(`residual_entry`) that downstream tooling references by name.
Internally, it's now treated identically to any other `Logical(_)`
module.

## Implementation outline

The rewrite has three parts; each is small in isolation:

1. **Build atomic units (Stage 1).** New module
   `analysis::atomic_units`. Takes the owner graph, returns a
   `Vec<AtomicUnit>` where each unit carries its member owners and
   inter-unit edges (in the projected DAG). Run once per chunk.

2. **Assemble factors (Stage 2).** New module
   `analysis::factor_assembly`. Takes atomic units + spec claims +
   `unassigned_mode`. Returns the complete partition. This is the
   primary output that the materializer consumes; replaces the
   current `Partition` construction.

3. **Compute proposals.** Refactor of `analysis::factorize` (which
   currently computes both the partition-of-residual _and_ the
   advisory cells in one tangle). The advisory output stays in
   `OwnerGraphReport.factorize`, but operates on the factorize graph
   defined in _Advisory proposals_ above — the residual_entry node
   isn't special.

The materializer changes are mostly deletions: remove the
residual-specific branches, replace the partition input source from
"the schedule's pre-built partition + residual fallback" to "the
factor_assembly output directly". `peelability` re-projection
similarly drops the residual branches.

## Migration notes

This is a substantial rework; staging:

1. Land Stage 1 + Stage 2 + materializer simplifications first, with
   the proposal output disabled (or unchanged from the current
   closure factorize). Verify the materializer's outputs are
   identical on the existing corpus.
2. Update the proposal computation to the supernode-based graph.
   Verify proposals on gaffer 78d928dca7 are at least as useful as
   the current output.
3. Introduce `unassigned_mode` setting; default to `catchall` so
   nothing changes by default. Add `mini_factors` mode + tests +
   docs.

Each step is a separate PR with its own e2e validation.

## Why this is the right shape

- **One pass produces the partition.** No "compute residual, then
  fix up" or "compute logical modules, then catch leftovers". The
  factorize is the partition authority.
- **One pass produces proposals.** The advisory output is the
  result of running the same closure-SCC machinery on the
  factorize graph, not a separate algorithm with its own gate
  reimplementations.
- **Modes are local.** `unassigned_mode` only affects how unclaimed
  atomic units roll up in Stage 2. Stage 1 doesn't change; the
  proposal computation doesn't change (except for the emit-block
  closure rule which is mode-(a)-only).
- **Residual loses specialness.** Every factor goes through the
  same materialization code path. The only thing distinguishing
  `residual_entry` from `helpers/chain` is the name.
- **Atomic units are stable.** They depend only on the chunk's
  source. The same atomic units survive across spec edits, which
  means peelability projections, factorize proposals, and validator
  diagnostics all reference a stable underlying unit of grouping.
