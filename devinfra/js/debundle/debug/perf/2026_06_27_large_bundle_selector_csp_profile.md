# Large Bundle Selector CSP Profile

This note summarizes a capped profile of the current global selector assignment
path on a large private downstream bundle. It intentionally avoids naming the
downstream product or copying any private source/code excerpts.

## Current Measurement

- A remote Bazel build with the downstream target and local Ducktape override was
  capped at 5 minutes. The build did not complete.
- The Bazel critical path included normal tool rebuild cost, but the debundle
  pipeline action itself was still running after about 99 seconds when the build
  was interrupted. That already crosses the "slow" threshold for production
  debundling.
- A direct local replay of the debundle pipeline action under `perf record` was
  also capped at 5 minutes. It did not finish.
- No CP-SAT request proto or CP-SAT summary JSON was emitted during the direct
  replay. That means this run did not reach the OR-Tools sidecar. The current
  blocker is pre-solver Rust work, not proven OR-Tools solving time.
- The run also appeared memory-heavy. We did not capture a reliable max-RSS or
  heap allocation profile in this pass, so memory needs to be measured explicitly
  on the next replay.

## Hot Path

The sampled direct replay points at selector assignment model construction:

- `lowering::materialize::materialize_logical_chunk`
- `ChunkPlanBuilder::resolve_and_claim_global_selectors`
- `selector_runtime::solve_global_selector_program`
- `selector_backend_solver::compile_backend_problem`
- `selector_constraint_model_builder::compile_selector_problem`
- pre-cutover typed domain materialization and validation

The dominant self-cost was `BTreeMap`/`BTreeSet` insertion while validating every
new finite-domain variable. The next visible costs were allocator churn, cloning
large selector/program structures, and a smaller AST purity scanner hotspot.

The important interpretation is not "the solver is slow." We are spending the
time before the solver request exists, while constructing and validating the
generic finite-domain model.

## Memory Risk

The current model shape likely creates both CPU and memory pressure:

- every variable gets a cloned vector of all values in its domain type;
- every variable validation allocates a temporary ordered set over that vector;
- allowed-table lowering repeatedly constructs sorted sets of typed tuple values;
- string-valued facts are cloned into domains and tuple rows before interning or
  backend encoding.

That means high RSS is plausible even before solver startup. The next replay
should be wrapped with an external memory measurement such as `/usr/bin/time -v`
to capture max RSS. If max RSS is high or grows steadily before the CP-SAT dump,
run a heap profiler such as heaptrack or massif on the direct replay. That keeps
profiling outside the production code and should show whether memory is dominated
by variable domains, tuple tables, string cloning, or backend request building.

## What We Still Do Not Know

We do not yet know the final CP-SAT problem size for this large bundle run:

- variable count
- domain-size histogram
- allowed-table arity histogram
- allowed-table row-count and cell-count histograms
- binary constraint counts by kind
- `all_different` counts and arity histogram

The current CP-SAT summary dump can report those once the backend is reached,
but the measured run timed out before that boundary.

## Rust-Side Algorithm Plan

The current Rust-side algorithm has three expensive shapes before OR-Tools can
do any propagation:

1. **Global domain cloning.** `FactDomains::values_for(domain)` gives every
   selector variable all known values of that type. If there are `V_owner`
   owner variables and `D_owner` owner facts, construction starts with roughly
   `V_owner * D_owner` typed values, even when each variable is immediately
   constrained by a selective atom.
2. **Repeated ordered-set validation.** The pre-cutover typed model validated
   each variable by inserting every domain value into an ordered set. The
   backend-copy bridge then validated again and rebuilt per-variable ordered
   sets for membership checks.
3. **Full relation scans and typed tuple sets.** Each atom lowering scans or
   filters a fact relation, clones typed values, inserts `Vec<ConstraintValue>`
   rows into a `BTreeSet`, and later converts the rows again into backend integer
   ids.

The next work should attack those algorithmic costs in this order.

## Root Cause

The hot `BTreeSet` insertion is a symptom. The deeper bug is that the Rust
constraint model treats every variable as owning an explicit `Vec` of all
possible typed values:

```
selector variable -> Vec<ConstraintValue>
```

For source-shaped selectors, many selector variables are AST-node variables.
Each one starts with `all AST nodes in the bundle` as its domain, even when the
selector immediately constrains it by kind, literal, child relation, owner root,
or another selective relation. For owner/string variables the same mistake
appears as `all owners` or `all strings`.

So "emit a variable" is not O(1). It is currently closer to:

```
O(number_of_variables * full_domain_size * log(full_domain_size))
```

because each variable:

- clones or copies the full typed domain into its own vector;
- validates that vector by inserting every value into an ordered set;
- gets validated again by `model.validate()`;
- gets converted again by the backend-copy bridge, which rebuilds per-variable
  ordered sets for membership checks;
- would later be serialized to the CP-SAT sidecar as another full repeated list
  of values, where C++ copies, sorts, and duplicate-checks it again.

That is why a large run can spend minutes before OR-Tools exists. The model is
not just "emitting variables"; it is repeatedly materializing and checking huge
cross-products of variables and global domains.

The correct shape is:

- global value dictionary / interned ids are owned once per fact domain;
- a variable domain is either a compact range/reference to a shared domain, or a
  narrowed candidate set;
- unary atoms and selective relations reduce candidate domains before variable
  materialization;
- allowed tables are built over those narrowed/interned domains;
- broad full domains should be represented as ranges or shared domain handles,
  not copied vectors per variable.

### 0. Make Construction Observable

Add a pre-backend, anonymized model-build summary before backend conversion:

- selector program counts: targets, variables, atoms, `all_different` groups;
- fact relation cardinalities by relation;
- initial global domain sizes by domain type;
- per-variable candidate-domain sizes after any Rust-side pruning;
- allowed-table arity/row/cell histograms before protobuf encoding;
- max RSS from the wrapper command, not from production timing hooks.

This summary must be emitted before OR-Tools request encoding so timeout runs
still tell us how large the Rust-built problem became.

### 1. Remove Validation/Conversion Waste

This is the safest first code change because it should not alter the CSP model:

- replace per-variable `BTreeSet` duplicate checks with linear validation over
  already-canonical domain vectors, or add an internal constructor for domains
  known to come from canonical `FactDomains` sets;
- avoid calling full `model.validate()` twice on the builder-to-backend path;
- replace backend per-variable `BTreeSet<ConstraintValue>` membership checks
  with sorted-vector/binary-search checks or integer-domain checks after
  interning;
- keep the public defensive validation path for tests and external construction,
  but stop using allocation-heavy validation on the trusted production builder
  path.

Expected effect: lower CPU and RSS without changing solver semantics. This is
not a solver heuristic.

### 2. Derive Per-Variable Candidate Domains Before Materializing Domains

The deeper algorithmic issue is the full global domain assignment. Instead,
compile selector atoms to exact support sets before constructing variables:

- for every atom relation, compute the support values for each variable column
  after constants are applied;
- initialize each variable's domain to the intersection of supports from all
  atoms mentioning that variable, falling back to the full typed domain only for
  genuinely unconstrained variables;
- filter allowed-table rows to those candidate domains before storing them;
- optionally run a fixed-point semi-join / table-constraint support pass: remove
  any value that has no supporting tuple in any incident allowed-table
  constraint, then repeat until stable.

This is ordinary CSP/table-constraint propagation at model-build time. It does
not change the solution set; it prevents Rust from allocating values and tuples
that no satisfying assignment could use.

### 3. Intern Values Before Tuple Construction

Move from typed, clone-heavy `ConstraintValue` rows to compact ids earlier:

- intern owner/node/string/ordinal values once per fact domain;
- represent relation rows and allowed tuples as small integer rows during
  lowering;
- keep a dictionary only for result projection and diagnostics;
- sort/dedup compact rows with `sort_unstable` + `dedup`, or prove the source
  relation is already canonical, instead of inserting typed rows into a
  `BTreeSet` per atom.

Expected effect: less string cloning, smaller tuple tables, less allocator
pressure, and a cheaper protobuf boundary.

### 4. Index Relations Instead Of Scanning Them Per Atom

For common atom shapes, build reusable relation indexes keyed by constant terms:

- owner -> declared binding/export/reference/member rows;
- string/member/property literal -> owners or AST nodes;
- AST node kind/literal/property posting lists;
- owner-owner/reference edges and ordinal relations.

Then lower an atom by reading the relevant posting list or indexed join rather
than scanning the whole fact relation. This is Datalog/database-style relation
evaluation, not exact assignment search.

### 5. Collapse The Double Model Representation

This has been implemented for the supported subset: the Rust boundary is now a
`CompiledSelectorProblem` with interned finite-domain ids, shared full-domain
sets, narrowed sparse supports, compact allowed tables, and direct protobuf
emission. Keep this shape; do not reintroduce a full typed model followed by a
full backend copy.

### 6. Only Then Tune The Solver Layer

If we reach the CP-SAT proto and the OR-Tools sidecar is the slow stage, switch
to a solver-specific investigation:

- profile the sidecar directly on the saved proto under `perf`/callgrind and
  capture max RSS;
- inspect CP-SAT solver stats: presolve time, conflicts, branches, propagation,
  wall time, solution count/support enumeration behavior;
- decide whether to change the CSP model shape, add redundant propagation
  constraints, add symmetry-breaking constraints, factor large allowed tables,
  use CP-SAT decision strategies/search hints, or tune CP-SAT parameters.

Those solver/model heuristics are valid if the solver is the measured bottleneck.
They should not be mixed into Rust-side model-construction work until we have a
saved problem and solver stats.

## Secondary Investigations

- Check whether selector program cloning in
  `resolve_and_claim_global_selectors` can be removed or made shallow.
- Check whether the AST purity scanner is recomputing facts that should be
  cached or generated once per bundle.
- Confirm that `all_different` remains a native backend constraint in the
  OR-Tools path, not an expanded pairwise constraint set.
- Once the backend is reached, inspect the CP-SAT histograms before making solver
  tuning decisions. If OR-Tools itself is then slow, profile the sidecar
  separately.
- Capture max RSS on the direct replay and, if memory is high, run a heap
  profiler before making large representation changes.

## Immediate Next Steps

1. Add the pre-backend anonymized model-build summary and max-RSS capture path.
   This makes timeout runs useful even before OR-Tools is reached.
2. Remove pure construction waste: cheap trusted domain validation, no duplicate
   full-model validation on the builder-to-backend path, and no temporary
   ordered sets for per-variable domain membership when sorted vectors or
   interned ids suffice.
3. Rerun the same capped private downstream replay under `perf` and external
   max-RSS measurement. Compare the profile against this note before attempting
   broader changes.
4. Implement per-variable candidate-domain derivation from atom support sets,
   then filter allowed tables before materialization. If needed, add a
   fixed-point semi-join support pass for table constraints.
5. Intern values and relation rows before tuple construction, then replace
   repeated whole-relation scans with reusable indexes for common atom shapes.
6. When the run reaches OR-Tools, record the actual CP-SAT problem-size
   histograms. If solver search is the bottleneck, replay the saved proto
   through the sidecar under a native profiler and use CP-SAT stats to choose
   model changes, backend optimizations, or CSP/search heuristics.
