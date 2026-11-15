# Constructive module placement

Status: architecture correction, 2026-11-15. This replaces the search-first
module-proposal plan. The prepared model, prediction and propagation contracts
below are the target design. The complete construction's coverage proof remains
a required design milestone; it has not been established by the existing tests.

Given an accepted partial spec, construct useful residual extractions from the
program's placement constraints. Keep authored owners fixed by default. The
objective is residual reduction with understandable ownership, rather than more
or smaller modules. Broader owner reorganization follows only when it unblocks
additional useful extraction.

W9 in [TODO.md](../TODO.md) is the dispatch item. The
[original tooling audit](../debug/2026_11_15_module_tooling_audit.md) preserves the
pinned inventory, counterexamples and source links. The
[current CLI guide](../docs/module_search.md) describes delivered behavior.

## Architectural decision and current state

Realizability is an analyzable property of a prepared dependency graph and a
complete placement. It should be inexpensive to predict without rerunning source
analysis, selector resolution or materialization. Planning must expose and use
those constraints directly, rather than learn them by asking whether arbitrary
complete assignments pass.

The existing batch checker, snapshot-bound plans and exact application are useful
foundations. The implemented complete proposer enumerates atom assignments and
calls that checker. Its finite coverage argument supplies a reference baseline;
it is not the intended production generation algorithm. Adding a generic solver,
beam, retry loop or faster checker around that enumeration would not by itself
correct the architecture.

The intended flow is:

```text
prepared program + accepted ownership + permitted moves
    -> placement constraints
    -> forced groups, forbidden owners, remaining relations
    -> constructive placement and import schedule
    -> exact placement and schedule certification
    -> reviewed selectors and names -> exact application -> materialization
```

Use existing typed graph, atom, assignment and gate objects. Add constraint and
query capabilities to them; do not create another serialized graph or parallel
interpretation of dependency kinds. The analyzer's assumptions remain unchanged. First make the current import-order
policy an explicit checked schedule without changing acceptance. Then derive
alternative schedules from the same constraints for placements the current
policy rejects. Preserve an already accepted schedule whenever possible.

## Problem and guarantee

Let `U` be source units, `A0` the accepted complete starting assignment and `R0`
the residual-origin body units. A result changes explicit destinations; omitted
units retain their exact owners. A peel has positive residual gain: at least one
unit of `R0` enters an explicit extracted owner. Moving code between generated
residual buckets does not count.

A structural atom is a required co-location group. It may contain several
bindings, declaration sites and anonymous statements. It is the smallest
placement variable, not a proposed semantic module. Several atoms may have to
move together for a particular destination without becoming one structural atom.

The initial scope permits whole residual body atoms to stay at their baseline
destinations, join allowed existing owners or enter new owners. Authored units,
import/export scaffolding and units excluded by an explicit restriction are
fixed. An atom containing a fixed unit is fixed as a whole. Generated residual
modules remain distinct from the true entry destination; only entry is the ESM
root that necessarily executes last.

Every returned graph-accepted plan must pass the exact complete checker. At least
one invocation must also satisfy:

```text
there exists a permitted accepted placement with positive residual gain
    => return an accepted witness

return exhausted_no_peel
    => no such permitted placement exists
```

The guarantee includes required-unit queries, joint multi-atom moves and several
destinations in one batch. It is relative to the prepared analysis and its
conservative gate, not every behaviorally equivalent JavaScript rewrite. Resource
limits, unsupported inputs and failures remain inconclusive or errors.

Graph acceptance is separate from selector realization and runtime evidence.
A graph witness may still lack a uniquely synthesizable selector. Report that
realization failure explicitly; it is not a no-peel conclusion. Saved plans bind
the exact graph/context snapshot. Application must preserve every omitted unit,
resolve the complete resulting spec and reject stale or changed assignments
before writing.

## What the realizability gate actually computes

The checker combines structural ownership with the shared realizability gate.
The relevant implementations are [atoms](../atomic_groups.rs),
[canonical endpoint projection](../graph/quotient.rs),
[canonical module edges](../graph/linker_order.rs),
[the gate and its incremental index](../realizability/mod.rs), and
[shared import ordering](../esm_import_order.rs).

For a placement `A`, derive two directed module graphs from the same source edges:

- `C(A)`: cross-module initialization constraints, including effective eager
  reads and sequencing obligations.
- `I(A)`: all emitted intra-chunk import dependencies, including lazy and hoisted
  reads and phantom side-effect imports. `C(A)` is a subgraph of `I(A)`.

Projection drops same-owner edges. Rebinding is checked as a separate
cross-owner violation rather than emitted as an import. Crucially, a fallback
promoted at-init edge whose caller remains in true entry is inactive; moving that
caller activates the edge. Preserve this condition from `project_endpoints`.
The lenient quotient used by some reports drops additional promoted edges and
cannot be the planner's validity graph.

The gate then checks:

1. No cross-destination rebinding; structural ownership must also remain intact.
2. `C(A)` has no multi-module directed cycle.
3. In mixed import cycles, the emitter's actual ESM evaluation visits each
   constraining dependency before its consumer.
4. No non-entry module has a constraining dependency into entry, which runs last.

The third condition uses the full import graph, not just eager dependencies.
The shared policy computes a dependency-first linker order from `C(A)`, reverses
that order within import SCCs for entry's imports, and orders other modules'
imports by linker position. Numeric module identity breaks remaining ties. ESM
DFS starts at entry, which imports every emitted module. Source-order refinements
in the emitter preserve the gate's dependency-order verdict.

Consequently, the desired equivalent acceptance formulation is:

```text
scope(A) AND total_ownership(A) AND intact_atoms(A)
AND no_cross_rebinds(A)
AND acyclic(C(A))
AND for every (consumer, dependency) in C(A):
      postorder_A(dependency) < postorder_A(consumer)
```

Here `postorder_A` is produced by the existing deterministic import policy and
rooted traversal, not a freely chosen topological order. Outside source import
SCCs, DFS already satisfies import dependencies; dependencies into entry are the
additional implicit-entry-fanout case. This explains the gate's SCC filtering and
explicit entry guard. Pin equivalence of this formulation and the current gate
before using it to derive generation rules.

This is global but bounded graph work. A changed placement can affect linker
ordering and expose TDZ in an SCC outside the changed owner. The existing remote
TDZ regression rules out destination-only certification. None of this requires
rerunning the source analyzer to predict a placement.

## Prepared model and query boundary

Prepare once per source/spec/assumption snapshot:

- The existing dependency graph, source-unit identities, atoms and unit-to-atom
  index, retaining edge provenance and activation conditions.
- The exact accepted assignment and complete destination table, including empty
  authored owners, generated residual owners and entry.
- Permitted destinations and fixed ownership from the requested move scope.
- Incident-edge indexes and the existing maintained constraining/import quotient
  buckets, SCC/reachability facilities and shared import-order machinery.

Keep the structural atomic condensation separate from placement-dependent module
condensations. Their SCCs answer different questions. Likewise, receiving more
residual units does not make an existing owner movable.

Provide three internal operations over this prepared state:

| Operation                                | Result and authority                                                                                                                                              |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Propagate partial placement requirements | Forced equalities, extraction obligations, forbidden destinations, unresolved relations, or a witnessed contradiction. Surviving choices are not certified valid. |
| Predict an exact assignment delta        | Full accepted/rejected verdict and typed source-edge evidence for the simultaneous final placement.                                                               |
| Construct a peel under a move scope      | A concrete checked witness, a proved exhaustive no-peel conclusion, or an explicitly incomplete result.                                                           |

These are capabilities behind `modules propose` and `modules check`, not three
new public commands. Reuse `AssignmentProblem` and `AssignmentAnalysis` at their
existing boundaries. Add typed constraint state only where there is a live query
or generator consumer; do not mirror report JSON into a new semantic model.

### Exact delta prediction

Extend the incremental index from its single-target hypothetical move to a batch
mapping source units to possibly several destinations. Compute all endpoint
changes against the same final assignment. Remove old incident-edge contributions
and add final contributions atomically; a rejected intermediate one-target move
must not invalidate an accepted simultaneous batch.

Preserve canonical destination ordering. Adding or renaming a fresh owner may
change numeric module identities and ordering ties. Maintain stable internal
handles with an explicit canonical-order mapping, or remap the maintained graph
consistently. Reusing old numeric IDs after inserting a path is incorrect.

Fast paths need whole-assignment proofs. Examples include an unchanged canonical
quotient and ordering on an accepted baseline, no constraining edges after
ownership checks, or import SCCs with no internal constraining edges plus no
constraining dependency into entry. Existing touching-only tier results do not
establish these global conditions.

Otherwise run the shared global order computation and simulator on the effective
module graphs. A full quotient traversal is an acceptable initial implementation;
add localization only with a proof of the affected region. Cache keys include
edge activation, graph topology and relevant destination ordering. An unchanged
local SCC alone is not a sufficient cache key.

Ordinary prediction returns a decision and concise evidence. Detailed rejection
reports are rendered on demand; they must not be rebuilt on every cheap probe.

## Explicit import scheduling

A placement does not uniquely determine a valid ESM traversal. Retain a typed
schedule containing ordered import targets for entry and every emitted module,
including phantom imports. The verifier checks the exact required target sets,
then runs actual ESM DFS and checks every constraining edge. A freely chosen
module topological rank is not a schedule witness; acyclic `C` alone is not
sufficient for schedulability of `I`.

Stage this change deliberately:

1. Materialize the current `EsmImportOrder` policy into a concrete schedule and
   prove equivalent acceptance. Share that retained object through
   `AssignmentAnalysis`, the gate, candidate translation/effect validation and
   lowering. Emission must consume the verified schedule, not recalculate one.
2. Construct alternative schedules from the canonical constraints only when the
   current policy fails. Verify every result with the same traversal verifier.
   Keep accepted existing order stable. Prove the constructor's coverage, or
   explicitly retain an incomplete status until that proof is supplied.
3. Retain the checked schedule witness in machine-generated plans when needed for
   exact replay. Keep generated schedules out of authored module YAML. Applying
   a plan checks its snapshot, complete placement and schedule before any write;
   ordinary materialization must derive the same accepted schedule.

Three real-source CLI probes obtained during design must become regressions:

- A residual atom placed after two fixed owners fails Pass 2 while placing it
  before them succeeds, with no constraining cycle or eager dependency into
  entry. This pins owner-name sensitivity of the original policy.
- An incoming eager consumer joining a different fixed owner repairs that
  extraction; either individual move rejects but the simultaneous batch runs
  correctly. Outgoing dependency closure alone misses this companion.
- Two required residual atoms have incompatible single-new-owner lexical gaps
  under the original policy, but two new owners work. This characterizes that
  policy, not an eternal requirement for two semantic owners after scheduling
  is derived explicitly.

Implement this scheduling work in this project. It is not a deferred permanent
TODO. Keep policy-characterization tests distinct from semantic impossibility
claims and add independent tiny import-list enumeration as the schedule oracle.

## Constructing the placement constraints

Use a worklist. Record each deduction with its source edges, fixed-owner premise
and move-scope premise. Revisit incident constraints when a destination domain,
required extraction or ownership equality changes.

The initial deduction rules are:

1. **Atom and ownership equalities.** Start with intact atoms and accepted authored
   owners directly. Do not rebuild them through fallible greedy contractions.
2. **Extraction implications.** An active constraining read from extracted code
   into a true-entry dependency is forbidden. If that dependency can move, record
   that it must leave entry too; if fixed there, reject the proposed requirement.
   The companion may join another module. Do not automatically merge it into the
   consumer's owner. Apply the rule to promoted edges that activation enables.
3. **Owner exclusions from ordering.** Propagate reachability through the forced
   constraining quotient. An owner choice that necessarily creates a directed
   cycle is forbidden. Use only edges and endpoint facts forced by current
   domains; possible edges cannot justify unconditional pruning.
4. **Conditional co-location.** A retained constraining path between endpoints
   required to share owner `M` forces its intermediate vertices into `M` too;
   otherwise the quotient contains a cycle. This is a placement-conditioned
   group, not an additional structural atom. Conditional edges retain their
   guards in this deduction.
5. **Domain exhaustion and singletons.** A contradiction eliminates the triggering
   requirement; a singleton owner domain becomes another fixed endpoint. Preserve
   alternatives such as “the dependency must leave entry, to any compatible
   owner” rather than arbitrarily choosing one and losing other completions.

For example, requiring atom `a` in owner `M` while `a` eagerly reads true-entry
atom `b` yields “extract `b`”, not “put `b` in `M`”. If `b` is fixed in entry,
that requirement is impossible. If the move necessarily adds `M -> N` and the
forced quotient already contains `N -> ... -> M`, owner `M` is forbidden for
that requirement. A merely possible path, or an edge a companion placement could
internalize or deactivate, is not enough to exclude it.

The analysis therefore returns relations between placements, not a table of
independently allowed atom/owner pairs whose Cartesian product is presumed valid.

Lazy/hoisted imports participate in import topology but do not create eager
extraction implications. A lazy reference into entry can be legal: entry bindings
are importable through generated exports. The old proposer's private-residual
policy is not a primitive gate constraint.

Cycle witnesses and TDZ witnesses must not be conflated. A constraining cycle
requires breaking the relevant cross-owner constraints; co-locating one suitable
group is one repair. A TDZ witness depends on the global ordered traversal.
Merging every module in a reported SCC is neither a necessary repair nor a proof
that all its source units form an atom.

### Constructive generation

Analyze the residual once to expose its dependency implications and the targets
excluded by fixed ownership. For a seed or required-unit request, propagate its
placement requirements to a fixed point and construct the required companions
and compatible owner relations. Share deductions between queries with the same
prepared context rather than recomputing every subset.

Generate from those constrained groups and relations. Preserve simultaneous
placements, including dependencies assigned to different owners; require no path
through individually accepted intermediate moves. Check a constructed concrete
result through exact delta prediction before labeling it graph-accepted. Rank
accepted results by residual reduction, disturbance and semantic review needs;
size preferences and display limits must not change the constraint model.

The seed/target worklist is a constructive mechanism, not yet a complete
algorithm. The following work is required before switching complete mode:

- Establish whether every permitted peel has a witness representable by the
  propagated closures and owner relations. Prove coverage; do not assume that
  trying each seed in each owner is sufficient.
- Derive what additional relations are needed for exact global ESM ordering.
  If the initial construction misses a peel, reduce it to a neutral source/spec
  counterexample and identify the specific missing relation. Rejecting one
  concrete ordering does not forbid all supersets or all placements of its seed.
- Resolve any remaining choices over those explicit relations. Do not assume
  a general search problem, introduce a solver first, or encode the checker as
  a black-box accept/reject callback. Any residual choice procedure needs a
  coverage argument and measured justification after direct propagation.
- Account for fresh-owner identity and relative ordering. Reuse the existing
  finite-representative argument until a stronger invariance theorem permits
  removing distinctions. Arbitrary fresh-name symmetry is not established.

These are open proof obligations, not permission to weaken the required complete
invocation. The design does not assert that exponential enumeration is necessary,
or that a closure-only procedure has already been proved complete.

## Cost model and observability

Separate preparation cost, propagation cost, exact prediction cost and selector
realization/materialization cost. The first three consume the prepared analysis;
only the final stages need source-resolution and output work.

A batch overlay examines incident source edges, then the effective quotient as
needed. Exact global fallback costs graph traversal and deterministic ordering
on modules/imports, rather than another source-analysis pass. Propagation should
visit changed constraints and reuse reachability or closure indexes where they
pay for themselves. Do not promise a constant-time global verdict.

Track work counts suitable for regression checks: preparations, incident edges,
propagation visits, exact quotient checks and global simulations. Use real opt
profiles for latency and memory, with large neutral or downstream workloads.
Do not turn each ordinary invocation into a verbose profiling report.

The key performance regression is structural: a chain or fanout whose obligations
determine its companions must be constructed through graph propagation, without
walking its power set. Increasing independent residual atoms must not multiply
full assignment checks merely to rediscover known implications.

## Proof and test obligations

Keep the independent tiny-spec enumerator as a test oracle. It enumerates raw
source-unit assignments and validates concrete specs through the normal pipeline;
it must not inherit the generator's atoms, closures, pruning or owner contraction
rules. The existing CLI oracle and shrinking properties are the starting point.

| Obligation                  | Required evidence                                                                                                                                                             |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Canonical-model equivalence | The constraint projection and all-edge postorder formulation agree with the full current gate, including promotion activation and entry fanout.                               |
| Deduction soundness         | Every independently accepted completion of a partial requirement respects each reported forced companion, equality and forbidden owner.                                       |
| Exact prediction            | Batch incremental verdicts equal the from-scratch full verdict, including rejected intermediate moves and remote TDZ.                                                         |
| Proposal soundness          | Every returned plan respects its scope, keeps atoms intact and passes concrete-spec validation.                                                                               |
| Discovery completeness      | Reference existence agrees with complete construction for every generated small spec/query, including each required-unit query. Add the general coverage argument separately. |
| Realization                 | Check, preview and apply preserve the whole intended assignment, including omitted units; accepted emitted fixtures execute with non-vacuous Node observations.               |
| Efficient construction      | Graph-determined chain/fanout fixtures bound propagation and full-check counts; profiles assess actual latency without brittle wall-clock assertions.                         |

Required cases include multi-unit and anonymous atoms; rebinding and local
mutation; eager versus lazy and hoisted reads; conditional promoted edges;
fixed-entry blockers versus generated residual-module dependencies; fixed-owner
extensions; joint multi-destination moves; accepted asymmetric import cycles;
Pass-1 cycles; Pass-2 rejection without a Pass-1 cycle; remote evaluation changes;
empty authored owners; fresh-name ordering; cut-atom scopes; size/output caps;
stale contexts; cancellation and exhausted budgets. Retain the existing cap
false-positive regression and its accepted grouped control.

Use real parsed sources and CLI fixtures wherever the case is reachable. Keep
constructed graph tests only for combinatorial cases awkward to express through
source; the current remote-TDZ graph witness is not a real-source CLI witness.
Property failures retain reproducible seeds and shrink to committed neutral
source/spec/query regressions. Tests support the proof; finite agreement does
not establish universal completeness.

## Delivery and documentation

The implementation sequence is:

1. Commit this complete plan and W9, including scheduling and the three neutral
   CLI regressions. Pin canonical-model equivalence before changing semantics.
2. Deliver shared whole-batch prediction with differential tests, preserving
   existing exact check/apply contracts.
3. Introduce explicit schedules preserving the current policy first; route the
   gate, translation/effect validation and emitter through the retained witness.
   Then derive alternative schedules with a coverage argument and independent
   raw import-list oracle.
4. Deliver graph-derived implications, owner exclusions and worklist propagation
   with completion-based soundness and structural work-count tests. Include
   incoming consumers, conditional promotion and joint owner/order relations.
5. Complete the construction and general coverage derivation across the entire
   permitted scope, including required queries and several destinations. Resolve
   only the genuine remaining choices exposed by constraints. This proof is a
   prerequisite for replacing the complete implementation.
6. Replace production Cartesian enumeration with the proved constructor and move
   exhaustive placement and schedule enumeration into test support. Keep honest
   found/exhausted/incomplete/error outcomes and scope/budget contracts.
7. Consolidate duplicate placement paths; deliver matching CLI, stable docs,
   AGENTS.md and repository-owned skills. Build skill archives, execute the
   packaged workflow and validate fresh downstream residual reduction while
   preserving authored owners and all omitted assignments. Consider bounded
   owner reorganization only after it demonstrably unblocks another peel.

The project is complete only when the production constructor and schedule path
meet those proof, independent-test, packaging and downstream obligations. Publish
reviewable commits on the existing PR, splitting dependent PRs if needed, and
report validation and exact-head CI separately.

Keep the currently delivered complete invocation until its replacement has both
the coverage argument and differential/CLI evidence. This migration constraint
does not make exhaustive enumeration the desired long-term implementation.
The old advisor remains advisory until migrated; an empty shortlist is never an
impossibility proof.

The public workflow is `inspect -> propose -> review -> check -> preview/apply -> run`.
Keep existing fixed-owner and destination-scope options; strategy selection must
not silently change permitted ownership edits. Do not add a menu of overlapping
search algorithms as a substitute for the shared analysis. `found` means an exact
accepted witness; `exhausted_no_peel` requires completeness; interrupted, limited
or unsupported analysis cannot masquerade as exhaustion. Application re-resolves
the entire reviewed spec and certifies exact assignment equality before writing.

Update `SPEC.md` and `docs/design.md` when the constraint/prediction contracts
ship. Update CLI help, `docs/cli.md`, `docs/module_search.md`, `docs/planning.md`,
`docs/spec_editing.md` and the README task chooser with the matching workflow.
`AGENTS.md` must pin the shared constraint model, global prediction, honest
negative outcomes and required oracle/property coverage.

Update repository-owned skills together: planning/orchestration query and scope
selection; intake's exact checked handoffs; worker/integrator application and
combined-batch checks; architecture/explanation roles' distinction between
placement constraints and semantic ownership. Review naming/stabilization roles
for changed shared context. Keep runnable recipes under `skills/shared/examples`,
build affected archives and exercise the shipped propose/check/preview/apply/run
recipe. Edit repository sources, not installed skill copies. Future behavior
stays in this plan until delivered; packaged agents must invoke shipped commands.
