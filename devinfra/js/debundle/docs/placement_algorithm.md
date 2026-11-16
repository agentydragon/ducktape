# Target placement algorithm, correctness and complexity

Active design contract: every materialized chunk supplies one shared import
priority. Ordinary compilation verifies that witness; it never searches for a
different order. The supported complete residual operation is the
baseline-preserving fresh-owner family proved below. This explicitly replaces
the earlier unrestricted placement promise. There is no partition-worklist or
arbitrary-scheduler fallback after this constructor.

This document states the graph model, algorithm and proof. Its obligations are
a design contract, not a claim that every implementation or consumer has passed.

## Model and contracts

The prepared input contains source units, structural atoms, the accepted complete
assignment A0, fixed owner identities, the true entry e, and a concrete accepted
baseline import schedule sigma0. Generated residual owners are distinct from e.
The source graph and `project_endpoints(..., EndpointView::Gate, ...)` are the
only authority for projection; diagnostics' lenient graph is insufficient.

For an assignment A, let I(A) be the actual non-rebinding import edges and C(A)
its initialization constraints. A C edge u -> v requires v to finish before u.
C is a subset of I. Entry additionally imports every emitted nonentry owner.
The chunk's `import_order` contains every emitted nonentry owner exactly once.
Every module sorts its actual canonical import targets by that same priority;
entry targets are implicit, and imports of entry sort first. The resulting
concrete schedule contains exactly those targets. Its DFS starts at entry.
An ownership subset has its own emitted owner universe: filtering a priority
from an accepted full assignment creates a new DFS witness that must be
verified independently.
The placement Gate also requires intact atoms and no rebinding across owners.

Fallback-promoted edges whose callee source unit equals their caller source unit
are inactive exactly when that caller belongs to true entry. Extraction must
activate them before deriving constraints. Neither a source-order preference nor
C acyclicity alone certifies the emitted traversal.

Three operations have different obligations:

| Operation                                                          | Required result                                                                                   |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| Verify A and its supplied shared priority                          | Exact acceptance/rejection of that witness, polynomial in their size.                             |
| Construct a baseline-preserving fresh peel                         | Sound and complete for the family defined below, with a polynomial bound.                         |
| Find arbitrary schedules, ownership reorganizations or other peels | Outside the supported complete operation; no implicit attempt or unrestricted no-peel conclusion. |

Source analysis, selector resolution and materialization are separate stages;
they cannot be hidden in a claim about prepared-graph query cost.

## One prepared context and certificate verifier

Prepare dense source-unit/atom/owner IDs, unit-to-owner assignments, incident-edge
CSR indexes, counted forward/reverse I and C adjacency, atom membership,
rebinding constraints, and the baseline certificate once. Keep public module
paths at the boundary. Empty owners and entry have explicit identities.

For a simultaneous move, visit the union of incident source edges once and
compare their old/final canonical contributions. Maintain parallel counts and
emit only final pair zero crossings. Compose changes until an order query is
actually needed; rejected owner-domain/cap states need no schedule maintenance.
Use maintained SCCs and invalidate certificates only for affected interiors.

A concrete certificate verifier:

1. Checks the complete owner map, all fixed/omitted claims and intact atoms.
2. Projects canonical I/C and checks that each import list contains exactly its
   emitted targets, including entry's universal fanout. The supplied priority
   must contain exactly the nonentry owner universe, including empty owners.
3. Executes one iterative DFS and records finish ranks.
4. Rejects a cross-owner rebind or a C edge u -> v with finish(v) >= finish(u).

**Correctness.** Steps 1 and 4 check the placement obligations. Step 2 prevents a
certificate from omitting an inconvenient import or owner. Step 3 is the actual
ESM traversal of those lists, so step 4 checks the required dependency-before-
consumer relation for every C edge, including dependencies into entry. Thus
acceptance is equivalent to the graph Gate for that exact map and schedule.
This is relative to the captured source analysis and assumptions, not a new
whole-language semantic theorem.

With dense IDs, full verification is O(U + M + Q + L) time and space, where U/M
are source units/edges and Q/L emitted owners/imports. Once canonical topology
exists, checking targets and traversal is O(Q + L + |C|). An incremental move
can often use only its d incident edges and r affected topology/certificate
region; O(d + r) is the efficient graph-work target, not a constant-time promise.
Worst-case r can be the whole quotient. Current ordered maps add logarithmic
factors. Finding a new arbitrary schedule is not part of this bound.

**Migration-only witness capture.** For an already accepted pre-contract
traversal, list nonentry owners in DFS discovery order. Sorting every import list by this list
reproduces its discovery and finish trace: at each recursive step the next white
target is the earliest undiscovered target in that list. Reordering already
gray/black targets adds no traversal events. Induction on the stack operations
proves replay of the whole trace. Thus a shared priority can express every
accepted traversal needed for migration. This does not make finding a priority
polynomial: discovering it still includes the hard scheduling problem below.
Normal materialization retains the author's exact priority, rather than
silently replacing it with a captured normalization.

## Polynomial baseline-preserving fresh peels

### Exact family

Existing owner assignments stay fixed. Each movable atom starts in true entry.
Choose a nonempty set S of these atoms and put them in one permitted fresh owner
x. Every omitted unit stays at its baseline owner. A query may require particular
atoms. Fix a permitted fresh destination x and require an effective fresh-owner
cap of at least one; otherwise this family has no permitted result. The remaining
construction is relative to that destination.

The witness family is precise:

- Preserve the relative import order of every baseline owner.
- Remove imports into entry only when canonical projection no longer emits them.
- Append x last to every baseline owner that now imports it, and last in entry.
- When x first runs, it may import black or gray baseline owners, but must not
  discover any white baseline owner. Every C dependency of x must be black.

Consequently the baseline traversal, projected onto old owners, is unchanged.
This excludes other import insertions, alternative traversals, moves out of
nonentry generated residual owners, and extension of a nonempty existing owner.
No negative result from this family covers those excluded operations.

### Construct constraints once

Record distinct discovery/finish events D(v), F(v) in sigma0. Postorder alone is
insufficient: an unfinished target may be gray or still white.

Use the finest canonical projection: one owner per movable atom, existing owner
anchors, and true entry. This activates fallback edges for every potentially
extracted caller. Edges out of an unselected caller become inactive again when
it returns to entry; rows out of selected atoms remain exact.

Build a residual closure graph K. For each C edge from atom a to movable atom b,
add a -> b. Add both directions for a rebinding relation between movable atoms.
Mark an atom blocked if it has a C dependency or rebinding obligation on fixed
entry code, a rebinding across a fixed nonentry owner, or a forbidden destination.
A selected set must be successor-closed in K.

For every movable atom a, define bounds from fixed nonentry owners:

```text
lower(a) = max({0}
               union {D(v) : a -> v in I}
               union {F(v) : a -> v in C})
upper(a) = min({F(e)} union {F(u) : u -> a in I})
```

Ignore I dependencies on entry, which stays gray. A C dependency on fixed entry
was already marked blocked. Fixed nonentry C consumers of selected atoms cannot
exist:
those edges would already have constrained fixed code to depend on entry in the
accepted baseline. Moving an entry atom does not newly activate a fixed caller.

For a group S use max lower(a), min upper(a), and OR of blocked(a).

### Derive all minimal closures without trying assignments

1. Compute SCCs of K and its condensation DAG.
2. Combine bounds/blocked flags inside each SCC.
3. In reverse topological order, propagate successor maxima/minima/blocked flags.
   These idempotent operations handle overlapping descendant closures directly.
4. With required atoms, combine their propagated bounds; otherwise inspect each
   SCC's bounds. A closure is feasible exactly when it is unblocked and lower <
   upper. Optional ranking changes which feasible result is returned, not validity.
5. Walk the DAG once to materialize the one selected successor closure. Do not
   enumerate all transitive-closure sets. Build its owner map and append-last
   witness, then independently certify that one finished result.

Pseudocode, after canonical preparation:

```text
if x is not permitted or effective_new_owner_cap < 1:
    return no_peel_in_this_witness_family
components, dag = condense(K)
summary = aggregate_local_bounds(components)
for c in dependency_first_order(dag):
    for d in successors(c):
        summary[c] = meet(summary[c], summary[d])

if required is nonempty:
    roots = components_containing(required)
    candidate = meet(summary[r] for r in roots)
else:
    roots = choose_one_feasible_component(summary)
    candidate = summary[roots]

if no roots or candidate.blocked or candidate.lower >= candidate.upper:
    return no_peel_in_this_witness_family
S = descendants_including_roots(dag, roots)
return certify(assignment(S), append_last_witness(S))
```

### Correctness proof

**Lemma 1: closure is necessary.** If selected a has a C dependency on an omitted
entry atom b, the candidate has C(x,e). Entry finishes last, so that constraint
cannot be satisfied. A rebinding crossing x/entry is also forbidden. Thus any
accepted S is successor-closed in K and avoids blocked atoms.

**Lemma 2: the summaries are exact.** An SCC must be selected as a whole by
successor closure. The condensation is acyclic. Induction in dependency-first
order shows each propagated summary is the max/min/OR over exactly that SCC and
all its descendants. Overlapping paths cause no error because these operations
are idempotent. Combining required roots gives the summary of their union.

**Lemma 3: the first new-owner visit has the claimed deadline.** Consider the
unmodified old-owner traversal. Appending x last to an old importer causes x to
be visited immediately before that importer's original finish event. Therefore
its first visit is at t = min F(u) over fixed I predecessors of S, or F(e) if
there are none. Removing an import into the already-gray entry does not affect
old traversal. With no white target discovered by x, induction over that trace
shows no other old-owner event changes order. Hence t = upper(S).

**Lemma 4: the window test is sufficient.** If lower(S) < t, every fixed I target
of x has already been discovered; it is black or gray and causes no new visit.
Every fixed C target has already finished and is black. Closure removes C/rebind
obligations on omitted entry atoms, and no fixed nonentry C consumer of x exists.
Old
fixed-to-fixed edges and their valid ordering remain unchanged. Internal selected
edges disappear under co-location. Thus the append-last witness satisfies every
atom, rebinding and C/I obligation. Independent full certification must agree;
a disagreement is an implementation error, not another candidate trial.

**Lemma 5: the window test is necessary for this family.** At t, a fixed target
with D(v) >= t would be white, violating the family's no-new-discovery rule. A C
target with F(v) >= t would not be black and would violate initialization order.
Thus every accepted witness in this family has lower(S) < upper(S).

**Theorem: soundness and family completeness.** Returned groups are accepted by
lemmas 1–4. Conversely, suppose a permitted S in this family exists. Choose any
atom in it, or all required atoms. Their least K-successor closure T is contained
in S by lemma 1. Removing atoms cannot increase lower, decrease upper, or add a
blocked flag. Hence T also satisfies the window test. Lemma 2 computes that test
exactly, so the algorithm finds a closure. This proves completeness for the
stated witness family, not for arbitrary schedules.

### Complexity

Let n/m be vertices/edges of the prepared finest quotient, including K. Computing
baseline clocks, local bounds, SCCs, the condensation and all summaries takes
O(n + m) graph operations and O(n + m) memory with dense adjacency. Materializing
one closure and witness is also linear. Required roots are combined once.
Current BTree-backed lookups add O(log n) factors. Source-index preparation and
one independent full certificate verification are additional linear passes over
their respective inputs. There is no partition enumeration or schedule retry.
Emitting every overlapping closure separately can require quadratic output;
finding one witness does not require doing that.

## Lessons for implementations

The earlier general placement worklist and exact scheduler produced valid
individual witnesses but did not provide practical unseeded discovery. One
prepared 1,073-atom run inspected eleven partitions in a ten-second budget;
a default run spent its budget in one constrained component. Later incremental
work still performed 133 million eager topology-edge visits before a scheduling
decision, and an unconstrained case reached a 3,898-owner import SCC with more
than 600,000 internal edges. These observations identify failed approaches,
not inherent lower bounds for residual discovery.

- Necessary contraction clauses and finite partition coverage can prove
  termination while leaving exponentially many choices. Faster containers,
  iterative rollback or cached component certificates do not change that
  complexity class.
- Successful-subtree commitment cannot bound failed nested retries; the hub
  reduction and clique-plus-isolated family in
  <polynomial_esm_scheduling.md> explain why. C acyclicity and original I-SCC
  reachability are insufficient.
- Eager incremental topology maintenance can dominate work before a candidate
  needs a schedule. Derive the supported family's decisions from one prepared
  constraint index instead of maintaining speculative general schedules.
- A passing hand-picked unit or an apply smoke does not establish broad
  discovery, and graph feasibility does not establish selector realizability.
  Measure an unseeded prepared query and exercise the selected plan through
  check, preview, application and relevant runtime behavior.

The former implementation record is preserved in
[PR #16644](https://github.com/agentydragon/ducktape/pull/16644). Its historical
full-suite pass and targeted downstream witness are evidence for that revision,
not acceptance of this explicit-order contract. Keep only constraints that help
future changes here; git and PRs retain the remaining chronology.

## Why the family does not cover arbitrary peels

The family deliberately preserves the baseline traversal. A valid general peel
may require x to discover a white fixed owner or to appear earlier in an import
list. Adding an independent selected atom can add a lazy import that makes a
previously unschedulable C-closed group schedulable. Therefore minimal C closures
and the interval summaries are not a complete characterization of arbitrary
Gate acceptance.

For example, fixed owners A/B have I edges A -> B and B -> A, with C={B -> A}.
Add x with C(x,A), and a lazy A -> x edge. No schedule works: A lazily reaches
two consumers of its unfinished value. Adding a lazy x -> B edge permits
x -> B -> A; A skips gray x/B and finishes first. A larger selected group can
supply that extra lazy edge. A proof that simply assumes adding code can only
hurt scheduling is false.

Root-dominator checks also are not sufficient. Original I-SCCs cannot substitute
for reachability in the currently white induced graph; gray ancestors can cut
paths. Keep counterexamples to both invalid reductions in the test corpus.

## Complexity boundary

The general schedule-existence problem is NP-complete even with acyclic C,
C contained in I and one import SCC; see the reduction and full proof in
<polynomial_esm_scheduling.md>. A shared priority can represent every DFS trace
by discovery-order replay, so requiring a witness does not make finding one
easy. Verifying the supplied witness remains polynomial.

This is not a hardness claim about the narrower accepted-baseline, single-fresh-
owner residual-existence query. The constructor above avoids the general problem
by fixing the old traversal and proving completeness only for its precise
append-last family. There is no arbitrary scheduler fallback.

## Integration and guarantees at the boundaries

Use one prepared context, canonical owner map, incident-edge updater, concrete
certificate verifier and resolved-claim writer for every supported operation.
Plans must retain the chosen ordering certificate as well as snapshot-bound
assignment changes. Check, preview, apply and materialization must consume that
same certificate; silently rerunning an unbounded scheduler after a cheap
proposal would defeat both the complexity and reproducibility guarantees.
The plan carries the complete final priority and ownership changes together.
Deleting or merging an owner requires updating that priority, and creating one
requires placing it explicitly. An all-entry chunk supplies an explicit empty
priority; a missing chunk entry is an error. These are apply inputs and must not
invalidate reusable semantic analysis when its semantic projection is unchanged.

For bounded graph-based construction, failure returns absence only within its
recorded witness family. For a wider requested scope, return inconclusive unless
a proved complete algorithm covers it. A structural contradiction can be a
polynomial rejection certificate; inability to construct an order is not one.
The supported operation has explicitly adopted the interval-family promise.
Existing-owner extensions, reordered baseline traversals and moves out of
generated/catchall residual owners need separate algorithms and proofs. A
corpus report identifies unsupported chunks rather than counting their omission
as exhaustion. A future optional heuristic must be explicitly requested and
named incomplete; neither validation nor complete residual construction invokes
one automatically.

Graph feasibility and executable spec realization are distinct. An all-seed
inventory can be complete for graph feasibility while reporting that it has not
produced executable selectors. A result containing an executable plan also
preflights the writer against exact source/spec identity and verifies that its
selectors resolve to the planned owner map. A writer failure must never be
reported as graph exhaustion. Plans from one baseline need not compose: after
applying one, prepare a current graph and derive the next plan.

Required verification: independent tiny assignment/import-list oracles; positive
CLI/apply/Node cases; interval boundary/gray-target and lazy-rescue counterexamples;
conditional promotion, rebind/atom, required-unit, owner-cap, generated-residual
and existing-owner cases; zero unrelated-component work; and unseeded real-corpus
measurement followed by one actual checked application. Exhaustive enumeration
belongs in these small test oracles.
