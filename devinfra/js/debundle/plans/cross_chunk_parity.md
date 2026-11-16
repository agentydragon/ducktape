# Bounded interprocedural analysis

Active plan for cross-chunk effect propagation, retained callbacks and ordered
availability evidence. This replaces the pool/replay optimization checklist;
it is the algorithm owner for this work. Implementation is paused pending
resumption. The design below is a target, not a claim about the current engine.

## Problem and contract

Compute a sound approximation of what each top-level source unit can read,
write, invoke, retain and publish during initialization, including code reached
through other chunks and opaque reentry. Use those facts, together with
initialization and completion evidence, to validate the evaluation order of a
proposed emitted ESM program. Chunk boundaries must preserve knowledge; they
must not change the transfer used for an otherwise equivalent call.

This is interprocedural abstract interpretation with points-to/call-graph
inference and effect summaries. Dependency-driven fixed-point solving computes
may-behavior; bounded ordered interpretation supplies additional proofs where
source order, singleton identity or initialization phase matters. Both use one
operation semantics and explicit author assumptions. A summary's existence is
not a normal-completion or provider-readiness proof.

Requirements:

- Accepted output preserves behavior under the stated premises. Imprecision
  produces conservative constraints, co-location or a clear refusal.
- Unsupported operations can remain havoc. Expanding supported shapes is useful
  without modeling all JavaScript. Never drop reachable effects to gain speed.
- Source analysis is independent of module assignments. Validation consumes a
  fixed candidate; neither phase performs speculative module-placement search.
- All analysis identities, domains, transfers, solver work and retained state
  have polynomial bounds in explicit input size. Operational limits can be
  stricter, but cannot substitute for a domain/algorithm argument.
- Reuse follows semantic ownership and consumed dependencies. No LRU or
  owner-local pool of whole-world executions is the foundation of convergence.

The [ESM scheduling complexity boundary](../docs/polynomial_esm_scheduling.md)
remains separate: exact general import-order feasibility is NP-complete.
Selector solving and complete placement search are also outside this analysis
bound. Do not claim that this plan makes every CLI command polynomial.

## Review findings to resolve

These are source-grounded mechanisms, not completed CLI reproductions of every
consequence. Establish the causal fixtures before changing their semantics.
Interpreter paths below are relative to `facts/sync_effects/`; linker and
schedule paths are qualified from the Debundle directory.

| Current mechanism                                                                                                                                                                 | Required change                                                                                                                                                                     |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `interpreter.rs::instantiate_allocation` creates a fresh activation during repeated speculative callback execution.                                                               | Use bounded abstract allocation/environment identities for may-analysis; reserve fresh execution identities for bounded ordered proofs.                                             |
| `invoke_callback_pool_once` repeats the whole pool until a broad `callback_pool_observation` stabilizes. Private allocations and unrelated heap changes can trigger another pass. | Solve explicit value, call and effect dependencies; wake consumers only when their input facts grow.                                                                                |
| `interpreter/invocation_replay.rs` validates and restores an entire `World`.                                                                                                      | Share transfer summaries and apply their effects to current inputs. Narrowing the existing replay key alone is unsafe: restoring its saved world could overwrite unrelated changes. |
| `program_activation.rs::record_program_cell_read` appends availability traces for assumed reads; `program_execution/schedule.rs` compares counted traces.                         | Define idempotent hypothetical obligations separately from ordered observations. Solver traversal multiplicity must not become execution multiplicity.                              |
| `source_identity/program_linker.rs` removes a node from its active stack after each export-star path.                                                                             | Evaluate a finite dependency graph once per query state; a recursion-stack guard does not memoize completed DAG nodes.                                                              |
| Source promotion expands read/rebind closures and materializes overlapping retained sets repeatedly. Source execution inputs rebuild immutable indexes for each consumer.         | Prepare direct source facts once, retain shared summary handles and propagate deltas before flattening.                                                                             |
| Callback parameter census uses the caller's `source_index()` for foreign allocation keys.                                                                                         | Every callable lookup, including formal count and capture layout, uses its defining source. A missing foreign lookup is not a zero-argument function.                               |
| Root rewind restores `invocation_count`.                                                                                                                                          | Separate semantic rollback from cumulative work accounting; include every retry in the cost proof.                                                                                  |

The foreign-formal lookup underapproximates the intended transfer. No accepted
incorrect emitted program was demonstrated for that finding in the review.
The assumed-trace issue couples precision to solver traversal; it is not itself
a demonstrated unsound acceptance.

**Correction to the previous plan:** do not build a ledger preserving every
fallback visit before deciding which events have semantic meaning. Repeating a
may-read at the same semantic cut adds no new obligation. Actual ordered reads,
different cuts, and changes such as initialized → reassigned → initialized must
remain distinguishable. Existing checks rejecting unknown/assumed availability
remain obligations; deduplication does not grant permission to accept them.

## Pipeline and semantic owners

```text
one SWC parse + exact premises/package/loader inputs
    -> immutable SourceProgram + direct operation IR
    -> canonical module/cell links + constraint templates
    -> bounded interprocedural may-analysis
    -> prefix-sensitive owner effects + bounded ordered proof refinement
    -> sealed source-analysis artifact
    -> assignment + concrete emitted ESM schedule
    -> availability/effect/placement validation -> emission
```

### 1. Prepare and link once

`SourceProgram` owns immutable source units, callable bodies, parameter/capture
layouts, direct reads/rebinds, allocation sites, property names and operation
sites. Derive them from the existing single SWC parse and manifest visitor.
Extend that shallow analysis when information is missing; do not add a second
AST census inside an interpreter or consumer. A consumer borrows prepared data
instead of rebuilding all registered sources' execution inputs.

Keep identities typed and source-qualified at every boundary. Resolve imports,
reexports and namespace members to canonical live cells, including default
exports. Cell identity, current cell value and initialization availability are
three different facts. Linking a hoisted function does not initialize the
lexical cells it may later read.

For export resolution, build query states such as `Export(chunk, name)` and
`Binding(chunk, local)`, with explicit dependency edges. Preserve explicit-export
shadowing, default exclusion from stars, canonical origin and ambiguity rules.
Evaluate an acyclic query graph in postorder once per state. Initially retain a
clear conservative refusal for unsupported reachable cycles; never memoize a
result computed under a temporarily cut recursion edge as globally complete.
Namespace-name queries share completed nodes, including diamond DAGs. Broader
cycle support later needs its own ESM resolution argument and causal controls.

Source-owner reachability is likewise a graph of direct dependencies. Preserve
initialization guards on edges instead of pre-flattening each owner's entire
transitive closure. A blocked gate records its dependent consumers; a later
initialization wakes them. Static possible reach is not current execution reach.

### 2. Specify a bounded abstract domain

Start with a context-insensitive interprocedural core. Add a fixed-depth context
only for a demonstrated extraction need; depth is a design constant, not a
source-controlled or CLI-unbounded parameter. Do not key contexts by whole
argument sets, complete heaps, recursive captures or all execution paths.

Use finite states keyed by `(body operation, semantic cut, bounded context)`.
A cut is a prepared initialization/control point, not a solver pass or a
runtime birth. Calls at a cut join their formals and returns; recursive calls
reuse the same bounded states. Heap/cell contents and current escape membership
are prefix-indexed, so potential facts from a later cut do not become current
values at an earlier one. Within a body, ordinary CFG input/output states and
loop-edge joins replace path enumeration and unrolling. Source-derived static
versions are allowed; a new version per solver visit is not.

| Component                          | Representation and bound rule                                                                                                                                                                   |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Values                             | Finite source constants and allocation/callable tokens, plus kind-specific unknowns. Computed strings/numbers widen instead of creating an unbounded constant universe.                         |
| Heap objects and environments      | Allocation/capture site plus bounded context. Repeated births join into summary objects with explicit multiplicity. Recursive captures are graph edges, not recursively expanded identity keys. |
| Properties, descriptors and arrays | Source-derived named slots plus unknown-key/index summaries. Exact dense rows have a fixed bound; overflow joins into a summary while retaining callbacks, aliases and accessor effects.        |
| Calls and formals                  | Finite callsite/target/context relations. Unknown-host arguments include all relevant available callback values and an unknown alternative, using the defining callable's formal layout.        |
| Effects                            | Idempotent read/write/invoke/retain/escape/havoc facts, indexed by semantic owner and cut as needed. Causes and premises are separate bounded evidence.                                         |
| Availability                       | Explicit initialized, uninitialized and unknown possibilities at a semantic cut. No declaration-form or callable-identity shortcut establishes readiness.                                       |
| Unknown flow                       | An unknown value retains the capability/reach of values it abstracted. Opaque invocation can discover their callbacks; widening cannot silently forget them.                                    |

A powerset of a finite atom universe is acceptable: its height is linear in
that universe. Enumerating a separate analysis context for every subset is not.
Write the complete atom/tuple inventory before implementing the solver. Every
new generated identity, relation arity, primitive domain and provenance field
must have a bound in source/input size, not just in the heap it happened to build.

### 3. Compute summaries through constraints, not execution replay

Lower each operation to monotone constraints over values, heap slots, captures,
call targets and effects. A callable summary is a shared set of constraints and
effect/return relations referring to its formals and abstract locations. Applying
it binds those inputs and contributes output facts. It is not a saved
`Outcome` or a cached resulting `World`.

Reuse one operation definition and builtin/premise table for the may transfer
and any optional ordered transfer. Each operation declares its reads, writes,
possible calls, abrupt outcomes and unsupported remainder. Unknown accesses,
getters, setters, proxies, coercions, prototype mutation and publication must
route through those operations, not side channels omitted from dependencies.

Use a worklist of constraint instances with reverse dependencies:

```text
instantiate bounded constraint templates
insert entry facts and explicit premises
while a constraint is queued:
    consume its pending input deltas against indexed current facts
    register every consumed dependency, including wildcard/absence guards
    join its output delta into the destination facts
    queue affected consumers only when a destination grows
seal results only after all reachable work is quiescent
```

Register-before-read, or an equivalent atomic protocol, must prevent missing a
new input when a watcher is installed. A newly discovered target or graph edge
gets both the existing facts and subsequent deltas. A stale result can never be
published as sealed. Queue order affects cost, not the fixed-point meaning.
Use delta propagation and indexed joins: copy/inclusion rules forward new rows;
join rules combine new rows with indexed existing partners. Do not rescan the
whole heap, callback pool or accumulated effect set for each insertion. Charge
matches to bounded fixed-arity tuples; duplicate derivations add no semantic
occurrence. Share a callee's effect relation across its callers rather than
re-running its body for each summary consumer.

Recursive calls are SCCs of this dependency problem. Analyze them to a fixed
point over the bounded domain; do not recursively invoke bodies to manufacture
new worlds. Join includes both skip and invoke possibilities at opaque reentry,
including repeated invocation and callbacks returned or retained by callbacks.
In a may-analysis, stores through summary objects/captured cells use weak updates.
A strong-update transfer requires a sound singleton proof and a monotone rule
when that proof is lost; otherwise keep the weak update.

### 4. Keep prefixes and ordered proofs explicit

A whole-program potential-call graph is useful infrastructure, but must not
make future callbacks or future lexical values available at earlier cuts.
Retain prefix-sensitive reach/escape and cell facts, indexed by bounded source
control points. Across chunks, use the same canonical cells and operation
transfers. Unsupported cyclic initialization remains conservative. Each queried
cut supplies current values and availability to the shared summary; it does not
rerun the provider to guess readiness.

Keep two semantic records:

- `MayEffects`: a joined set of hypothetical effects and availability obligations
  at a semantic cut. Inserting the same fact twice is idempotent. Discovery
  order, fallback retries and diagnostic contributor count are not its meaning.
- `ExecutionEvidence`: observations justified by the bounded ordered interpreter,
  with source operation, scheduling owner, cell, availability and path/phase
  witness. Distinct real reads and transitions such as A/B/A remain ordered.
  Provenance refers to a bounded witness DAG; it does not enumerate all call
  paths. Unsupported loops/paths produce an obligation or havoc, not an invented
  complete trace.

The ordered interpreter may prove singleton/fresh-object properties and exact
initialization order. Its result refines a may-summary only with a sealed proof
covering the affected domain, dependencies and all relevant branches. An
incomplete execution contributes its conservative remainder and cannot erase
may-effects. This is one semantics with a proof-producing precision mechanism,
not two competing evaluators whose fields happen to be merged.

Replace the counted assumed-trace comparison with a specified comparison of
these two records. Compare source and candidate schedule at corresponding
semantic cuts; preserve observed values/availability and unresolved obligations.
Do not accept merely because two overapproximations happen to be equal, or
because repeated assumptions disappeared. Prove the replacement comparator's
sufficiency before changing gate authority. Keep the existing gate during the
migration, with explicit discrepancy evidence rather than a union of verdicts.

Bound ordered work explicitly: use a checked polynomial primitive-step allowance
per analysis, including nested execution, every alternative and all root retries.
Charge before expansion/copying and keep the charge outside semantic rollback.
Intern program-point paths as parent links; do not copy the full ancestor vector
at each child. Keep only bounded live snapshots and never flatten a compressed
trace into an unbounded event list. A receipt's precise allocation IDs cannot
create new may-analysis context keys.

### 5. Share dependencies and retire data by ownership

Immutable source/callable summaries live with `SourceProgram` for the analysis.
Solver facts and dependency indexes live through their final downstream use.
Ordered worlds and proof scratch live within the owning prefix/validation run;
branch snapshots are released when their join/consumer finishes. Freeze the
saved artifact, then release construction-only indexes. There is no recency
policy deciding which semantic computation exists.

| Data                                       | First computation                          | Retirement                                                                                                           |
| ------------------------------------------ | ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| Source indexes/link graph                  | Preparation/link phase, once per input     | After the final analysis/serialization consumer releases `SourceProgram`.                                            |
| Callable constraint instance               | First reachable demand for its bounded key | After the solve is sealed and its last summary/artifact consumer finishes. New target discovery reuses the instance. |
| Prefix/heap facts and reverse dependencies | First activation of their static cut       | After the last downstream cut/validation consumer; retain them while new edges can discover a consumer.              |
| Ordered branch state/proof scratch         | Entering its root or branch                | At the completed join/root, except the sealed evidence needed by validation.                                         |
| Materialized query/report view             | Its actual consuming query/output          | Immediately after that consumer; retain shared semantic handles, not an unbounded query history.                     |

Retained reachability consumes shared component/bitmap handles. Overlapping
roots reuse their common closure; union handles stay shared through source
promotion and effect consumers. Materialize only when a consumer actually needs
elements, and charge the materialized size. Avoid computing all-pairs transitive
closures merely to serve a sparse query. Dense required output can still be
quadratic; repeatedly copying it is not required.

Depend on specific cells/slots, descriptor and array surfaces, native-integrity
facts, initialization gates and escape membership. Include unknown-key,
prototype and global wildcard dependencies where a narrow footprint is unproved.
Absence of a property/accessor is also an input; an unchecked negative lookup is
not an immutable fact. Never derive a reusable must/absence proof merely from
a currently empty may-relation. Such proofs require closed immutable inputs or
a sealed version with dependencies revalidated at use. A new retained edge or a changed export must wake every
consumer that used the previous surface, including those that saw it empty.

Inside a monotone solve, facts and dependencies only grow. Model an ordered
replacement at a distinct bounded control point/state version; do not delete
facts from a solved global lattice. Branch restoration switches immutable world
handles and preserves the work ledger. If a separate derived view really needs
deletion, batch changes and recompute its affected region with a stated bound.
A simple sound polynomial rebuild is an acceptable first version; elaborate
fully dynamic SCC maintenance is not a prerequisite. Unrelated source indexes
and immutable summaries survive it.

### 6. Seal, persist and validate

Seal owner effects only after their summaries and required prefix dependencies
are complete. Save semantic facts, unresolved obligations, explicit premises and
bounded proof witnesses. Solver queue entries, visit counts, whole-world replay
records and process-local allocation identities are not artifact semantics.
Respect the existing source/projection/engine identity and SWC-context codec;
update saved readers and writers together when the representation changes.

Apply prepares its own source context, restores sealed facts and validates the
specific emitted schedule plus structural placement/vendor/loader obligations.
No graph receipt or purity bit silently becomes authority for a different proof.
Author commutation/effect promises discharge only their stated obligations and
remain distinguishable from inferred facts. False promises are outside the
conditional guarantee; known unexplained semantic differences get minimal linked
regressions, not undocumented exceptions.

If a transfer cannot remain precise, join a sound havoc summary for its reachable
effects. If that closure cannot be represented or computed within the bound,
return a sticky terminal resource/unsupported failure. Never accept a partial
fixed point, reset exhaustion by rewinding, or manufacture readiness to proceed.
Loaded artifacts also retain their independent size/resource checks.

## Polynomial bound and correctness obligations

Let `n` count explicit source, premise and resolved input bytes. Fix the analysis
schema and context depth. Let `P(n)` bound primitive fact positions, `Q(n)`
constraint instances, `E(n)` dependency registrations, and `T(n)` the maximum
cost of one transfer evaluation including joins, key comparison and allocation.
All four must be independently polynomial in `n`. A transfer can scan a bounded
relation; it cannot enumerate paths, argument subsets or expanded recursive
values. A fact position's identity includes its source/control-point/context
coordinates, so creating another cut is not a free new universe.

The implementation review must fill in this inventory, with concrete exponents:

| Quantity                                                 | Required derivation                                                                                             |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Sites, constants, names, bodies and source links         | Count from parsed input; widen generated primitive values.                                                      |
| Contexts, heap/environment/cell IDs                      | Fixed-arity tuples of input sites and fixed context depth. No fresh ID per solver visit.                        |
| Points-to, capture, call, effect and prefix relations    | Fixed-arity products of the preceding universes; account for each cut.                                          |
| Dependencies and SCC membership                          | Bounded keys/edges; include dynamic target discovery and dense closures actually retained.                      |
| Ordered steps, observations, live worlds and witness DAG | An explicit polynomial step/snapshot bound, charged cumulatively; include materialization and failed attempts.  |
| Serialization, comparison and diagnostics                | Bounded shared input traversal plus requested output bytes; no implicit expansion of compressed multiplicities. |

**Solver bound.** The joined fact domain has at most `P(n)` strict insertions.
Each constraint is evaluated initially and after a consumed input grows; with
queue deduplication there are at most `Q(n) + E(n)P(n)` evaluations. Thus the
solve costs `O((Q + EP)T)` time and `O(P + Q + E)` retained solver state, plus
bounded indexes and witnesses. SCC computation/rebuilds are separately charged
to bounded edge updates. Ordinary fixed-arity relation joins satisfy a polynomial
`T`, though a poor join order can still be expensive. This is not a near-linear
performance claim or an IFDS distributivity claim.

**Other phases.** Bound preparation, linker query states, ordered work and output
explicitly and add them to the solver bound. If there are at most `L(n)` ordered
steps/snapshots of size at most `S(n)`, even a loose `O(LS)` snapshot-space bound
is polynomial; persistence improves the actual cost without being the proof.
All numerical budgets use checked arithmetic. A cap stated only in generated
heap size does not establish any of these input-size bounds.

**Correctness.** Before adoption, establish these obligations:

1. The abstract states at each cut overapproximate concrete states reachable
   there, under exactly the recorded premises. The allocation/environment
   abstraction preserves all aliases; `Many` never receives singleton treatment.
2. Every operation transfer overapproximates normal and abrupt outcomes,
   callbacks, mutation and publication. Join and widening preserve inclusion;
   prefix/initialization gates cannot admit future values early.
3. Dependency registration is complete. Monotone iteration from entry facts
   terminates at a post-fixed point containing all reachable concrete behavior,
   including recursion and opaque reentry. Induct on concrete transitions to
   establish containment, rather than inferring it from termination alone.
4. Ordered refinement has a valid simulation/coverage argument for every fact it
   removes or strengthens. Work exhaustion retains the conservative remainder
   or fails; partially explored paths never establish completion.
5. The schedule comparator and existing placement/loader obligations imply
   observational equivalence for accepted output. Worklist reordering or
   duplicate fact discovery changes neither these obligations nor acceptance.
6. Saved analysis restores the same semantic identities, premises and evidence
   as fresh analysis. Artifact reuse cannot introduce a missing dependency.

These are a proof blueprint, not a completed whole-engine proof. The domain
inventory and the operation/comparator arguments are deliverables. A performance
plot, a finite-height claim without a bound, or passing corpus tests is not a
substitute.

## Migration and review boundaries

Keep each step independently reviewable. Independent source-preparation, linker
and regression-test work need not wait for the new solver. None of these steps
requires an early gate flip or removal of the useful ordered interpreter.

| Step                          | Deliverable and exit criterion                                                                                                                                                                                                                         |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1. Specify and reproduce      | Domain/cost inventory; define may versus observed records and comparator obligation. Minimal causal CLI controls for foreign formals, fresh callback allocation, assumed-read multiplicity and export diamonds. Record current limitations explicitly. |
| 2. Prepare/link once          | One borrowed source index per source; finite export/binding query graph; defining-source lookups. Single/split-chunk and saved/fresh controls agree, including refusals.                                                                               |
| 3. Separate semantic records  | Typed idempotent `MayEffects` and ordered `ExecutionEvidence`, consumed without eager flattening. Retain current gate until the new comparator's proof and negative controls pass. Drop the preserve-every-fallback-visit ledger proposal.             |
| 4. Establish the finite core  | Allocation-site/environment abstraction and monotone constraint solver for supported operations, recursion and callback returns. No fresh solver identities or argument-set contexts. Havoc/refuse unsupported shapes explicitly.                      |
| 5. Connect dependencies       | Prefix-sensitive call/escape/availability facts; shared source/retained summaries; complete mutation/absence/wildcard subscriptions. Unrelated writes do not invalidate a summary; relevant writes and initialization transitions do.                  |
| 6. Integrate proof refinement | Ordered lane consumes shared transfers/summaries with cumulative work accounting. Remove whole-pool replay and whole-world restoration from summary solving; retire superseded paths after their real consumers migrate.                               |
| 7. Seal all consumers         | Update saved DTOs, reports, graph/purity/availability consumers and validation together. Any change of authority gets its own discrepancy audit and behavioral acceptance evidence.                                                                    |
| 8. Demonstrate the result     | Close the concrete bounds; complete identical-input generated and canonical performance/behavior runs. Explain every accepted-boundary gain or loss, including remaining havoc.                                                                        |

## Behavioral and scaling acceptance

Each semantic regression uses the real CLI:
`bundle + spec -> emitted JavaScript executed under Node / emitted properties /
appropriate failure`. Analysis assertions can supplement the oracle. A candidate
fix must first reproduce its reported failure on the pre-fix engine. Neutral
fixtures retain the mechanism rather than original minified names.

| Family              | Positive control and adversarial boundary                                                                                                                                                                              |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chunk parity        | Equivalent local and two-chunk calls; transitive imports, aliases and namespace/reexport routes; foreign callback formals actually affect the observed result.                                                         |
| Bounded births      | Repeated allocating factory and recursive returned callbacks converge; callbacks from initial and later objects remain reachable. Distinct live allocations must not be conflated in a singleton proof.                |
| Mutable captures    | Callback reads a captured cell before and after a write; shared captures and two closure environments; skip/invoke and repeated reentry.                                                                               |
| Initialization      | Future lexical initializer, hoisted function over a future cell, cyclic/default live export and host-setter reentry. Bad reordered splits refuse; accepted colocated/safe-order controls execute correctly.            |
| Observations        | Repeated may-discovery at the same cut is idempotent; actual repeated reads, A/B/A transitions and different cuts remain distinguishable. Gate outcome is independent of worklist/discovery order.                     |
| Dependency coverage | Property addition/replacement/deletion; own/inherited accessor; descriptor/array/prototype change; unknown key and relevant native-slot mutation; formerly empty reachable set later receives a callback.              |
| Reach and widening  | Newly escaped callback, callback-returning callback, namespace replacement and unknown value retaining an old closure; unrelated primitive publication must not gain receiver-mutation authority.                      |
| Graph shape         | Wide shared captures, growing cycles and 48-layer two-wide export-star diamond; default exclusion, explicit shadowing, ambiguity and unsupported-cycle refusal.                                                        |
| Proof boundaries    | Dynamic scope, derived construction, opaque host/package loader cuts, loops/try/await/yield and unsupported receivers retain their required conservative behavior.                                                     |
| Failure/persistence | Resource exhaustion is terminal even after branch/root rollback, with no accepted partial output; supported neighbor succeeds. Fresh and saved analysis yield matching accepted behavior/refusal and semantic reports. |

Use existing descriptor, namespace, registration, superclass and callback suites
as controls; do not loosen accepted fixtures or raise caps to hide a regression.
Add narrow pure-function tests only for algebraic/solver invariants difficult to
observe through the CLI: join laws, monotonicity, complete watcher installation
and the stated work bound. Use test-local operation counts or existing work
accounting, never production timing instrumentation.

For scale, vary owners, callbacks, graph depth/width, shared overlap and relevant
versus unrelated mutations separately. The sparse shared case must avoid
repeating a full closure per callback/owner; dense output is charged once to its
actual consumers. Verify structural work against the proof and measure optimized
runs with real profilers. Do not infer polynomiality from a few timeouts or
claim a whole-engine speedup from a reachability microbenchmark.

Complete the unchanged canonical Excalidraw spec and upstream-icons candidate
under the ordinary gate, with Node/browser evidence, then the private Claude CLI
and Tana analyses plus apply/runtime checks. Compare the original engine and
candidate on identical source/package closure, semantic projection, compiler
flags, thread count and machine. Verify the executable digest; a remote test does
not necessarily refresh the local CLI artifact. Record completed artifacts, CPU,
wall time, peak RSS and source-analysis versus scheduling costs. A capped profile
without an analysis artifact is diagnostic only. Existing measurements and the
reproduction procedure remain in the
[performance investigation](../debug/2026_11_15_maintained_spec_resource_limits.md).

## Avoid and close out

Do not introduce whole-world revision convergence, fresh IDs on fixed-point
visits, path/subset enumeration, recursively expanded captures, global escaped
sets that erase prefix timing, unproved narrow havoc, count-preserving speculative
ledgers, or caches keyed by a convenient subset of actual dependencies. Do not
parallelize replay to conceal duplicate work. Do not claim byte-identical saved
facts are required when deliberately correcting their semantic representation;
compare external behavior and reviewed semantic differences instead.

This plan owns the algorithm and its remaining work. Cost measurements belong
in the existing performance report; artifact-cache work stays in
<staged_pipeline.md>; operation/capability migrations stay in
<abstract_interpreter.md>. When this plan closes, move the proved domain,
algorithm, complexity and comparator contracts into maintained inference/design
docs and `SPEC.md` as appropriate, remove its TODO pointer, then delete the plan.

Standard background: the [rustc dataflow framework](https://rustc-dev-guide.rust-lang.org/mir/dataflow.html)
illustrates the separation of domains, transfers, fixed-point solving and result
inspection. Our input-size bounds and ordered-proof obligations above are
additional requirements; a lattice top alone does not prove finite height.
