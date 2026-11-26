# Consolidate Debundle analysis from devel

Baseline: `origin/devel` at
[`ba0c3367d339a3eb30ae00ee575f656f5e562951`](https://github.com/agentydragon/ducktape/commit/ba0c3367d339a3eb30ae00ee575f656f5e562951).
The inventory and decisions below come from that tree. This planning change
branches directly from it and has no dependency on #16762 or #14552. Those PRs
are candidate material to examine, not implementations that must be preserved
or landed first. Implement this plan through bounded migrations; the complete
redesign and the real icons extraction are not prerequisites for an individual
slice.

The goal is to consolidate existing inference and operation semantics while
extending cross-chunk effects. Do not add another general-purpose fixed-point
framework or parallel semantic implementation. This plan owns the consolidation algorithm and stage dependencies;
other plans retain their separate capability, artifact-cache and gate-migration
obligations. <../TODO.md#remaining-dispatch-order> owns active dispatch;
<analysis_performance.md#open-analysis-tasks> owns the remaining PF1, PF4
impact-profile and PF5 cost tasks;
<analysis_performance.md#pass-standardization-tasks> owns PS1–PS4 common pass
contracts and infrastructure. Both retain concrete completion criteria. The
design below is a target, not a completed correctness proof.

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

## Existing machinery and its disposition

The inventory groups four overlapping production paths and two experiments;
it does not mean six interchangeable solvers. Some differences are
necessary: may-value flow, a positive capability certificate and an ordered
execution witness answer different questions. Consolidate their semantics and
dependencies without collapsing these distinct judgments into one safety bit.

| Existing owner on devel                                                                                                   | Keep / extend                                                                                                                                                                                                      | Retire or forbid                                                                                                                                                                               |
| ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `inference/iterative.rs`: `IterativeInference`, `IterativeRules`, `FactContext`                                           | Keep the production scheduler, watched reads, joins, completed strata and proof dependencies. Extend delta consumption only where current changed-key support is insufficient.                                     | A second production worklist copied from the prototype; a generic new solver beside this one.                                                                                                  |
| `analysis_vocabulary/value.rs` and `facts/value_flow.rs`: `Value`, `ChunkValueFlow`, `FlowRules`, `CandidateRules`        | Extend the existing finite origin/value relations to canonical program-qualified identities, imported values and needed heap/capture facts. Retain the source-only completed stratum and its real consumers.       | A parallel points-to model that separately reinterprets the same bindings, assignments, parameters and returns.                                                                                |
| `purity/effect_inference.rs` and `cross_module_purity.rs`: `PurityEffectRules`, primitive-export and callable projections | Keep the already program-wide bottom-up effect closure and explicit call obligations. Make purity/export capabilities projections of shared operation facts; preserve module-state and initialization obligations. | Independent body interpretation in projection code, or a claim that devel has no cross-module fixed point.                                                                                     |
| `facts/semantic_inference.rs` and narrow alias/native/loader rule families                                                | Keep positive certificates and typed premises over closed source/native facts. Keep distinct domains when their questions differ.                                                                                  | Duplicated source censuses and bespoke body recognizers once the shared transfer can prove the same fact. Missing evidence must not certify absence.                                           |
| `facts/sync_effects/runtime_inference.rs`: `RuntimeRules`                                                                 | Keep bounded prefix/request/result orchestration where needed. It uses single-producer immutable states; treat that as an execution DAG, not the recursive may-analysis fixed point.                               | Using fresh request IDs or imperative replay to simulate convergence of semantic summaries.                                                                                                    |
| `facts/sync_effects/kernel/`, `world/`, `alternatives/`, `adapters/`                                                      | Keep the ordered proof mechanism, canonical `LocalBody` lowering, shared world operations and application/provider adapters. Adapt it to consume the common facts and summaries.                                   | Another general evaluator inside an adapter, provider-summary projection or cache. Adapters are not separate engines to delete.                                                                |
| `facts/sync_effects/legacy/{evaluator,call_transfer}.rs`, flat root/call lanes                                            | Retain only the behavior required by unmigrated consumers while porting each operation to the shared semantics.                                                                                                    | Delete superseded evaluator arms and flattened reruns with their last consumer. No permanent second evaluator behind a fallback flag.                                                          |
| `x/summary_effects/{model,generate,effects}.rs`: `points_to`                                                              | Salvage constraint patterns, regression fixtures and the lessons from stable abstractions, delta propagation and SCC summaries. Audit them against the production domain and assumptions before porting.           | Its separate production scheduler/model, blanket prototype/key assumptions, copied transitive-summary vectors and dense all-pairs tables as defaults.                                          |
| `x/summary_effects/tier0.rs`                                                                                              | Keep useful counterexamples, reference-reach tests and individually sound classification ideas as inputs to shared transfers or diagnostics.                                                                       | Adoption as an authoritative gate while its default-export, abrupt-completion and optional-rule counterexamples remain. Remove redundant prototype paths after useful material is transferred. |

The production worklist is already usable infrastructure. `cross_module_purity.rs`
already closes local/imported purity obligations across participating modules;
it also retains structural value flows for later consumers. The missing work is
a coherent extension of those facts to the effects and prefix availability needed
for extraction, with explicit authority at each consumer. Performance and
correctness are not established merely by moving everything into one store.
The purity closure derives effectfulness and typed dependencies in the shared
worklist; callsite reasons are materialized once after closure. It is not a
complete heap-effect transformer. Preserve that useful judgment while deriving
it from the shared, richer operation facts.

### Baseline defects and limits to address

- `ChunkValueFlow` explicitly models local value identities, not arbitrary
  cross-chunk values. Imported purity/callable capabilities cover a narrower
  question than complete synchronous effects and live-cell availability.
- `invoke_possible_callbacks_transfer` repeats the callback pool against an
  evolving world; `callback_pool_observation` compares broad heap/native state
  and revisions. Replace this semantic replay with bounded fact propagation.
- `Value::Alloc` already denotes source-site abstractions. Its documented lack
  of multiplicity must be accounted for before a consumer uses a singleton or
  fresh-object proof; do not replace it with a fresh identity on every solve.
- Root-attempt checkpoints restore `invocation_count`. Semantic rollback and
  cumulative cost accounting must have separate ownership.
- Flat evaluation and the ordered kernel implement overlapping operation
  semantics. A failed root attempt can rewind and rerun through the flat lane;
  identify the legacy consumers before claiming either lane can be removed.
- The points-to experiment has the right worklist ingredients but documented
  semantic gaps and poor large-input cost. The tier-0 experiment has recorded
  incorrect commutation judgments. Neither is a verified replacement.

These are source/audit findings. Causal CLI reproductions and matched performance
runs are work to complete, not results supplied by this planning change.

### Remaining invocation delta work

Tracked as [PF1 in the analysis performance tracker](analysis_performance.md#pf1--incremental-invocation-targets-and-arguments).

The indexed invocation transfer, its exact-expression/late-edge distinction,
source-ordered activation semantics, proof propagation and polynomial work/state
bounds are maintained in
[Source invocation origin deltas](../docs/inference.md#source-invocation-origin-deltas).
The implementation no longer loops over old targets or replays unchanged
argument reads into whole-set formal joins. Remaining PF1 work is the matched
optimized corpus measurement in the performance tracker; do not infer runtime
improvement from the deterministic delivery controls.

### Evaluate unmerged work separately

The program-qualified runtime activation IDs, `program_execution` availability
traces, `source_identity/program_linker.rs`, whole-world `invocation_replay.rs`
and maintained retained-graph kernel reviewed on #16762 are not baseline files
or contracts at the devel revision above. Their design issues must not be
reported as facts about devel. In particular, fresh speculative births and
counted assumed-read traces are constraints to avoid when choosing what to port.

For each candidate hunk from #16762, record the devel gap, minimal causal test,
proposed owner in the table, invalidation/availability obligations, and code it
replaces. Prefer independently useful source-qualified facts and regressions;
do not import the PR's whole runtime, replay or graph architecture as a package.
The icons regression is one acceptance case for this work, not a reason to
preserve that branch's implementation.

## Pipeline and semantic owners

```text
existing SWC parse/manifest + exact premises/package/loader inputs
    -> shared prepared source + canonical identities/operation facts
    -> extended value/effect rules on IterativeInference
    -> bounded cross-chunk may-analysis
    -> prefix-sensitive owner effects + bounded ordered proof refinement
    -> sealed source-analysis artifact
    -> assignment + concrete emitted ESM schedule
    -> availability/effect/placement validation -> emission
```

### 1. Prepare and link once

The existing preparation/manifest and `SourceWithAst` owners retain immutable
source units, callable bodies, parameter/capture layouts, direct reads/rebinds,
allocation sites, property names and operation sites. Consolidate access to
those existing owners; `SourceProgram` below names that ownership boundary, not
an instruction to add a competing source model. Derive missing facts from the
existing single SWC parse and manifest visitor.
Extend that shallow analysis when information is missing; do not add a second
AST census inside an interpreter or consumer. A consumer borrows prepared data
instead of rebuilding all registered sources' execution inputs.

Keep identities typed and source-qualified at every boundary. Resolve imports,
reexports and namespace members to canonical live cells, including default
exports. Cell identity, current cell value and initialization availability are
three different facts. Linking a hoisted function does not initialize the
lexical cells it may later read.

Extend the existing cross-module import/export resolution rather than installing
a second linker. Use bounded query states such as `Export(chunk, name)` and
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

Extend the existing context-insensitive value/effect core. Add a fixed-depth context
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

Choose evaluation order from the dependency structure. For a sealed graph of
union/inclusion constraints, compile its edges once, collapse strongly connected
components, and propagate each completed component summary across each DAG edge
once. Account for summary/bitmap size in addition to graph work. More general
transfers can require iteration within a component; growing call/property
relations require indexed delta propagation and backfill when an edge activates.
These are evaluation strategies within the shared semantic framework, not
parallel analyzers or a requirement to run every closed stratum through a generic
worklist. Sealed candidate reachability is compiled through SCCs; PF1 and PF5 track the
remaining growing-graph and callback propagation work. PF4 tracks only the
matched impact profile for compiled capability containment; its structural
implementation and correctness bounds are documented in
<../docs/inference.md#prototype-capability-containment>.

| Dependency shape                             | Evaluation strategy                                                            | Work to account for                                                        |
| -------------------------------------------- | ------------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| Sealed acyclic inputs                        | One pass in dependency order, after prerequisites complete                     | Linear vertex/edge scheduling plus transfer and output costs               |
| Sealed union/inclusion graph                 | SCC condensation, then bottom-up summaries                                     | Linear graph construction/ordering plus summary unions and output costs    |
| Sealed graph with general monotone transfers | Dependency order between components, iteration only where required inside them | Component-local fact growth, joins and scheduling                          |
| Growing call/property relations              | Indexed joins over new facts and newly activated edges                         | New tuple matches, prior-fact backfill, proof changes and queue operations |
| Ordered program points                       | Explicit acyclic transfers between prefix states                               | Primitive steps, owned snapshots and transfer/retirement work              |

A general notification engine can match bottom-up graph work when it schedules
completed prerequisites and coalesces changes appropriately. An arbitrary
notification order does not guarantee this: it can repeat transfers, and an
ordered worklist can add logarithmic queue cost. Choose and justify the strategy
per pass; shared semantics do not require shared scheduling overhead. Merely
showing that both implementations are polynomial does not justify extra passes.
Keep the actual source execution order explicit whichever strategy is chosen.

Extend `FlowRules` and the existing program-wide effect/callable rules with
monotone constraints over values, heap slots, captures, call targets and effects.
Keep the typed domains separate where their joins differ, under the same
production inference framework. A physical global store is not required for
independent closed strata; shared semantic ownership and complete dependencies
are required. A callable summary is a shared set of constraints and
effect/return relations referring to its formals and abstract locations. Applying
it binds those inputs and contributes output facts. It is not a saved
`Outcome` or a cached resulting `World`.

Reuse one operation definition and builtin/premise table for the may transfer
and any optional ordered transfer. Each operation declares its reads, writes,
possible calls, abrupt outcomes and unsupported remainder. Unknown accesses,
getters, setters, proxies, coercions, prototype mutation and publication must
route through those operations, not side channels omitted from dependencies.

Where growing facts require reverse dependencies, use the existing
`IterativeInference` for bounded constraint instances.
Its existing `evaluate_changed` API tracks changed keys; add per-relation deltas
only with proved join semantics and meaningful work-count controls. The required
behavior is:

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

### Next bounded callback slice

The [November 16 checkpoint](../debug/excalidraw_pipeline_performance/README.md#november-16-consolidation-checkpoint)
finds substantial remaining callback/getter cost, but does not establish a
speedup or attribute all of that cost to one source shape. Start with
`e2e/callbacks/iteration_receiver.rs`: preserve a confined dense receiver's
own slots when a selected `forEach` callback passes only primitives to opaque
code. Change its safe refusal to an accepted extraction with the same Node
output. Keep the escaping-receiver refusal. This is one capability migration,
not a prerequisite to finish the entire callback engine.

The [receiver-confinement prerequisite investigation](../debug/2026_11_17_receiver_confinement.md)
identifies the missing shared complete-remainder effect/reach relation and its
consumers. The selected native row, local fallback, top-level settlement and
graph fallback must consume the same capability; preserving a slot in only
one of those boundaries cannot establish accepted extraction.

Admission must establish all of the following from shared prepared facts and
operation transfers, without another AST recognizer or evaluator:

- A fresh, singleton ordinary dense receiver has the queried own data slot;
  its allocation/multiplicity, descriptors and current-prefix identity are
  proved. The native method is selected before invocation and has a supported
  row. Initially support `forEach`, which constructs no species-dependent
  result array; other rows and nested/recursive execution remain conservative.
- The complete callback operation graph covers every reachable branch and
  call. An opaque operation's explicit operands are primitive. Its global,
  throwing and reentry effects remain represented. Unused implicit receiver
  operands do not become values supplied to that operation merely because the
  interpreter placed them in the callback's parameter environment.
- Neither those operands nor existing escaped/exported/reentry roots reach the
  receiver, its aliases or a callable that can access it. Resolve live captures,
  parameter aliases, `arguments`, rest, `this`, returned values and foreign
  surfaces, or refuse. A missing edge in an incomplete may-graph is not a proof
  of confinement; dynamic scope and unknown hidden private reach refuse it.
- Every write, escape, deletion, descriptor/receiver-prototype change and
  uncovered operation that may touch the receiver invalidates the proof before
  reuse. A separate intrinsic mutation may still invalidate a native-row
  prerequisite: preserving an own slot does not preserve all native premises.

Represent a successful result as a typed capability with the receiver, semantic
cut, protected slots and consumed dependencies. The shared operation semantics
owns the effect of the opaque operation and the native row; the existing
`IterativeInference` owns their dependencies. Keep an incomplete normal-execution
receipt incomplete. Preserve the slot only after proving that _all_ unexecuted
remainder effects are disjoint from it; do not skip a statement and resume its
old world on the strength of a read summary. Apply the same boundary to fallback
reach, so an unused implicit receiver cannot publish its stored callbacks.

For a concrete first implementation, bound contexts to the existing source
cuts and canonical source allocation/callable/binding identities. Refuse shapes
that require new dynamically generated contexts. Let `P` be the number of such
cuts, `R` the protected receiver candidates, and `V,E` the nodes and edges of
the completed fixed-arity capability graph at a cut. All are polynomial in the
explicit input; pin the exact tuple inventory in the implementation review.
Reach and invalidation are graph problems: dense-bitset propagation can compute
all `R` receiver reach sets in `O(R(V+E))` time per cut and `O(RV+E)` space.
This deliberately loose upper bound gives `O(P R(V+E))` total time. Boolean
receiver-validity facts change at most once at each cut. Effects and provenance
must use the existing bounded atom/witness domains, not path lists or subsets
as context keys. This bound excludes ESM schedule/placement search.

Share the immutable graph and body operation summaries. Index every consumed
cell, capture, surface and wildcard guard; delta propagation wakes only affected
receiver queries. A new edge receives existing reach bits as well as future
deltas. Sealed cut results live until their last downstream consumer, then can
be retired; do not retain resulting worlds in a cache or copy a transitive
closure into every caller. The whole-graph bound above is a ceiling, not a
reason to rescan the graph on every operation or use an LRU.

The preservation argument is local: fresh own-slot evidence establishes the
invariant; each covered operation either preserves it by its shared transfer
or invalidates the capability. Opaque code cannot access a confined receiver
without a capability path; all such paths and reentry roots are overapproximated
by the completed graph. Thus a retained capability proves the slot unchanged
on every continuation reaching the later read, regardless of whether the call
normally completes. Unknown reach invalidates that argument and keeps the
current fallback. This is the proof obligation for the proposed slice, not a
claim that the current implementation meets it.

Add unsafe controls for lexical/parameter aliases, earlier escaped/exported
callbacks, actual receiver writes and descriptor/prototype changes, getters,
replaced methods, recursion and truncation; test fresh/saved analysis parity.
Re-run the identical full-pipeline checkpoint. Do not broaden accepted shapes
or claim Excalidraw savings from the small case alone. If the existing shared
facts cannot establish confinement within this slice, document the precise
missing relation and stop instead of turning it into a whole-engine rewrite.

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

The existing ordered kernel may prove singleton/fresh-object properties and exact
initialization order. Its result refines a may-summary only with a sealed proof
covering the affected domain, dependencies and all relevant branches. An
incomplete execution contributes its conservative remainder and cannot erase
may-effects. This is one semantics with a proof-producing precision mechanism,
not two competing evaluators whose fields happen to be merged.

Specify comparison of these two records without introducing counted hypothetical
traces as semantic evidence. If a candidate implementation such as #16762 has
already introduced that representation, replace it rather than preserve it. Compare source and candidate schedule at corresponding
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

Implement every slice on current devel, in an isolated worktree, with its own
reviewable diff and causal tests. No slice depends on merging #16762. Independent
source/identity, regression and cost-accounting work can proceed without an early
gate flip. Before extending an analysis, name its existing owner and consumers;
before removing one, name the shared facts that replace its decisions.

A slice can migrate one existing analysis while preserving all of its current
conservative answers. Its exit criteria are the identified producer/consumer
migration, deletion of the superseded path, behavioral controls and bounds for
that phase. If gaining precision would require another analyzer's redesign,
retain havoc and leave that extension as separate work. Do not make the full
icons extraction or a whole-engine performance repair a prerequisite for these
bounded consolidations.

| Step                            | Deliverable and exit criterion                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Freeze the devel baseline    | Inventory producer/consumer ownership and existing cross-module capability coverage; run causal CLI controls and completed devel performance baselines. Define the domain inventory and may/observed comparison obligation. No runtime from a draft PR is a prerequisite.                                                                                                                           |
| 2. Reuse source and identity    | Build effect and availability migrations on the shared `ProgramBindings` and source-qualified value-flow store (docs/inference.md); add namespace and direct re-export routes only with their own resolution and capability controls. Keep one prepared source/index per source. Local/two-chunk and fresh/saved controls must agree, including refusals. Keep availability separate from identity. |
| 3. Extend existing facts        | Add needed cross-chunk value, capture, heap and effect relations to the production rule families. Bound every generated identity and context. Use explicit havoc/refusal for unsupported operations; no parallel semantic model or competing general worklist.                                                                                                                                      |
| 4. Replace callback replay      | Prefix-sensitive call/escape/availability facts and indexed deltas replace repeated whole-pool invocation. Cover late edges, formerly empty surfaces, returned callbacks and mutable captures. Finish the input-size/work bound for this path.                                                                                                                                                      |
| 5. Converge operation semantics | Adapt ordered proofs, purity, imported summaries and graph consumers to common transfers/facts. Define idempotent may obligations and real ordered evidence. Port and delete each overlapping legacy evaluator arm with its last consumer; preserve conservative behavior until replacement is sealed.                                                                                              |
| 6. Seal and retire              | Update saved DTOs, reports and all consumers together. Remove superseded flat/root reruns and prototype-only machinery once their useful tests/rules have moved. Any authority change is a separate discrepancy-audited gate migration.                                                                                                                                                             |
| 7. Demonstrate the result       | Complete the concrete bounds, generated scaling and unchanged canonical behavior/performance runs against the frozen devel baseline. Explain every gained/lost boundary and remaining havoc. Promote enduring contracts to maintained docs and close this plan.                                                                                                                                     |

The stages above describe dependencies and final obligations, not another PR
queue. Independent work proceeds under the dispatch order in <../TODO.md>.
Measurement and retirement accompany each slice; the final demonstration
confirms their combined result rather than postponing those checks.

For each slice, the review records its source of reused code, input/output facts,
consumed dependencies, work/space bound and work-count controls, removed duplicate
decisions, remaining legacy consumers and their removal gates, relevant
assumptions, CLI behavior and exact checks. The common completion contract is in
<analysis_performance.md#per-slice-completion-contract>.
Temporary parallel comparison is diagnostic and has a named removal criterion;
it must not become another permanent effect producer.

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
and Tana analyses plus apply/runtime checks. Compare frozen devel and each
candidate directly; #16762 is an optional third comparison, never the only
baseline. Use identical source/package closure, semantic projection, compiler
flags, thread count and machine. Verify the executable digest; a remote test does
not necessarily refresh the local CLI artifact. Record completed artifacts, CPU,
wall time, peak RSS and source-analysis versus scheduling costs. A capped profile
without an analysis artifact is diagnostic only. Existing prototype measurements and their limitations are in the
[prototype report](../debug/summary_effects/README.md); existing
production measurements are in
[the Excalidraw report](../debug/excalidraw_pipeline_performance/README.md).
Neither supplies a fresh devel-versus-candidate completion measurement.

## Avoid and close out

Do not introduce whole-world revision convergence, fresh IDs on fixed-point
visits, path/subset enumeration, recursively expanded captures, global escaped
sets that erase prefix timing, unproved narrow havoc, count-preserving speculative
ledgers, or caches keyed by a convenient subset of actual dependencies. Do not
parallelize replay to conceal duplicate work. Do not claim byte-identical saved
facts are required when deliberately correcting their semantic representation;
compare external behavior and reviewed semantic differences instead.

This plan owns consolidation of the existing analyses and its remaining work. Cost measurements belong
in the existing performance report; artifact-cache work stays in
<staged_pipeline.md>; operation/capability migrations stay in
<abstract_interpreter.md>. When this plan closes, move the proved domain,
algorithm, complexity and comparator contracts into maintained inference/design
docs and `SPEC.md` as appropriate, remove its TODO pointer, then delete the plan.

Standard background: the [rustc dataflow framework](https://rustc-dev-guide.rust-lang.org/mir/dataflow.html)
illustrates the separation of domains, transfers, fixed-point solving and result
inspection. Our input-size bounds and ordered-proof obligations above are
additional requirements; a lattice top alone does not prove finite height.
