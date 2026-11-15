# Constructive residual placement and exact import schedules

Design derivation, 2026-11-15. Concrete schedules, shared validation/emission,
and constraint-driven placement construction implement the algorithms below.
Delivery evidence and remaining acceptance checks are tracked in
`plans/module_placement.md`. The arguments establish the algorithmic contracts;
finite tests independently check the implementation and its edge cases.

## Model and boundary

Use the existing structural atoms, accepted assignment A0, destination table,
canonical `project_endpoints(..., EndpointView::Gate, ...)`, and typed source
edges. A0 must be accepted and atoms intact before construction. The constructor
may pass through rejected intermediate partitions; A0 is the scope reference,
not a required intermediate point in the contraction sequence.

An allowed final placement keeps fixed units and authored ownership fixed. Each
whole movable residual body atom stays at its baseline graph destination, joins
an allowed extracted existing owner, or joins an allowed fresh owner. Required
atoms must enter extracted owners. Generated residual destinations are distinct
from true entry. Their emitted filenames are layout labels: rematerializing after
extraction may renumber them. All existing owner anchors, including empty
authored owners, persist.

For a concrete partition, C is the initialization graph and I the full emitted
import graph. Project same-owner edges away. The ONLY additional Gate projection
activation condition is the fallback promoted edge
`PromotedAtInit { callee_source_unit }` with `callee_source_unit == edge.from`:
it is inactive exactly when its caller block is true entry. Do not use the
lenient quotient or the broader cross-module promotion rule. Multiple source
edges can support the same quotient edge; retain that provenance.

Entry imports every emitted nonentry module. Nonentry imports are exactly I
successors. A valid schedule is an actual ordered target list for every module,
whose ESM DFS postorder satisfies dependency-before-consumer for every C edge.
C acyclicity alone is insufficient.

## Exact scheduler with successful-subtree commitment

First preserve the current policy if its concrete schedule verifies. Otherwise
reject constraining edges into entry and decompose the nonentry I graph into
SCCs. Process the condensation in dependency-first order. Components without
internal C edges retain their preferred module import lists without search.
For each remaining component, construct an internal traversal using only its
internal I and C edges. Possible first roots have no incoming internal C edge.
The following component algorithm is exact, without enumerating all import-list
permutations.

Maintain white/gray/black colors, active stack, and a reversible schedule trail.
At frame u, only choices among currently white actual I successors can affect
DFS. Process or append nonwhite targets deterministically; they do not recurse.
All colors and trial import lists are local to the current component. External
dependencies have already finished; imports into true entry skip the active
entry. These external targets are restored to each completed module's import
list. Entry first imports the chosen component roots in dependency-first order,
then its remaining required targets, which have already been visited.

Before trying white v, compute R = Reach(I induced by current white vertices, v).
Reject this trial if there is a C edge w -> a with w in R and a either v or a
current gray ancestor. Otherwise enter v and recursively attempt to complete its
frame. When a child trial succeeds, COMMIT that entire successful subtree and
restart consideration of remaining white successors. When a trial fails, roll
back its entire subtree, including successful descendants within that failed
trial. If a full pass over current white successors finds no successful trial,
the frame fails. When none remain, finish u, checking its C successors are black,
and return success. Prefer the existing policy's successor order for determinism.

Checking the active-ancestor part of the entry rejection is useful defensively;
with the reachability invariant it is already implied by successful ancestor
entry checks. For v itself, the test is equivalently an incoming C edge to v
from within v's SCC in the current white I graph. Do not substitute roots of
arbitrary SCCs for actual available import successors.

### Scheduler proof

Any DFS subtree starting at v visits exactly R, independently of successor order:
all its initial white I-reachable vertices are visited before it returns, and it
cannot enter an initially white vertex outside R. Thus the entry rejection is
necessary: every w in R must finish before v and every active ancestor, violating
w -> a when a has not finished.

The key commitment lemma is stronger than mere necessary root constraints. A
successful valid subtree on R cannot destroy the existence of a valid completion
of its parent frame. R is successor-closed among the initial white vertices. If
a valid completion existed before this subtree, run the successful R traversal
first, then use that completion with visits to R omitted. No edge out of R can
have been needed to enter an outside white vertex. Visits into R now skip black
vertices. Relative finish order of outside vertices is preserved. Constraints
from outside into R become satisfied earlier; constraints from R to outside
white vertices do not exist; constraints from R to active ancestors were rejected;
and constraints within R are satisfied by the successful trial. The modified
execution is therefore a valid DFS completion.

Induct on the number of white vertices to prove frame completeness. If a valid
completion exists, its next white child has a valid subtree and recursive trial
will succeed. Trying other children first either fails without state change or
commits a successful subtree, which preserves existence by the lemma. Each
success reduces the white set. Consequently failure after all currently possible
children fail is exact, and there is no need to backtrack over previously
successful sibling subtrees. The returned target lists are still checked by the
independent schedule verifier.

This proves exactness, not polynomial complexity. Failed nested trials may repeat
substantial work. Instrument them, memoize only full relevant states, and use
independent tiny raw-import-permutation oracles.

### Independent SCC formulation and UNSAT evidence

Reject any C edge from a nonentry module into entry first. Treat entry as already
gray and remove it from the nonentry graph. Compute I SCCs. The whole graph is
schedulable iff each nonentry SCC admits a valid internal traversal with arbitrary
choice of its first root. For sufficiency, visit SCCs in dependency-first order
from entry, selecting each SCC's valid root and traversal; its outgoing targets
are already black, and imports into entry skip gray. For necessity, consider the
first entry into an SCC in any valid global traversal: all its members finish
before that traversal returns, and deleting downstream excursions gives a valid
internal traversal. C edges between SCCs follow dependency-first order.

This equivalence supplies an intrinsically UNSAT SCC S: every possible first root
fails its exact local scheduler. A failure at one entry point or one partial DFS
state alone is NOT such a certificate.

The constructor applies this decomposition before recursive trials. Independent
components therefore do not enlarge a trial's visited sets, constraint scans or
rollback state. The global work is decomposition, bucketing and final schedule
verification; recursive search depends on individual constrained components.

## Finest partition and domains

Start with one block per existing fixed owner anchor (collecting all its fixed
atoms), plus one separate block per movable atom. Do not put movable atoms into
their baseline anchor at initialization. Every permitted final placement is a
coarsening of this partition, and this is the coverage invariant.

An unanchored block is an optimistic distinct prospective module, not a committed
fresh path. Its domain contains the intersection of its atoms' allowed existing
anchors and a boolean allowing a fresh owner. Each atom initially allows its exact
baseline anchor plus permitted extracted existing anchors, and fresh ownership
when the query allows it. Required atoms exclude baseline residual/entry anchors.
A block containing a fixed anchor has only that anchor as its effective domain.
Never merge distinct fixed anchors. Domain intersection, fixed-anchor membership,
and required extraction must hold for every contraction. Empty intersection
rejects that contraction. A block with no fresh allowance must eventually join an
allowed existing anchor; a singleton possible anchor is forced.

Fresh names are symmetric for existential schedule acceptance: any bijective
relabeling of nonentry module IDs induces bijections of C, I, and concrete ordered
schedule lists and preserves DFS/postorder. Therefore one canonical set of fresh
paths suffices after exact alternative scheduling is supported. Prefer accepted
old schedules, but do not make their numeric tie-breaking an impossibility rule.
If `--into` restricts fresh paths to a finite supplied list, use distinct names
from that list and cap the number of fresh blocks by its size as well as the query
cap. Current query scope gives every movable atom the same allowed fresh-path
set. A future API with different per-atom fresh-name domains requires matching
constraints and a separate proof; the boolean domain alone would be insufficient.

## Constraint worklist and necessary contraction clauses

After every contraction, project the canonical quotient and apply the necessary
deductions below. The current constructor rebuilds this projection from the
prepared source graph; the separate batch-prediction API maintains incident-edge
buckets. Dedicated routines detect forced equalities and generate compatible
contraction alternatives. The report retains branch-category counts, not a
provenance record for each individual clause. Each branch disjunction below is
necessary as a whole; its individual alternatives need not be sufficient.

1. Structural ownership is already contracted. Any remaining symmetric mandatory
   ownership edge forces endpoint equality.
2. An active C edge u -> entry forces u to merge with entry. No schedule can fix
   it. This is a graph derivation of leave-entry companion requirements in the
   finest representation: a required extracted atom cannot merge with entry, so
   a forced merge can immediately refute its requirement.
3. Every active directed C SCC forces ALL its vertices to share an owner,
   including edges that can deactivate through fallback promotion. Let f map
   current blocks to owners in a valid final placement, and let r be the final
   schedule's postorder rank, with true entry last. For every current C edge
   u -> v, r(f(u)) >= r(f(v)): co-location gives equality; a surviving edge
   gives strict inequality; and a deactivated fallback edge has f(u) equal to
   true entry, so the inequality still holds. The canonical Gate projection only
   deactivates such a fallback edge when its caller is true entry. Directed
   paths both ways in an SCC force equal ranks. Distinct owners have distinct
   ranks, hence every SCC vertex has the same final owner. Contract the entire
   SCC and intersect domains; incompatible fixed anchors prove contradiction.
4. A block with fresh disallowed must merge with one allowed existing anchor.
5. If more than K blocks remain unanchored, choose any K+1 of them. Every final
   placement with at most K fresh owners must either merge a pair in this chosen
   set, or merge at least one selected block with an allowed existing anchor.
   This is the pigeonhole contraction clause. It need not mention every block.
6. For an intrinsically UNSAT nonentry I SCC S, form W as its full forward
   reachability closure in the nonentry I graph. Every valid completion must
   contract some pair of distinct current blocks with at least one endpoint in W.
   Include allowed partners both inside AND outside W, including owner anchors
   and true entry; filter by domain compatibility. This clause is broad but sound.

The last clause requires its forward closure, not merely S. An outside merger
can add a routing edge that repairs S without identifying two S vertices. For
example C has A->V and B->V, and I has V->A,V->B; it is unschedulable. Adding a
lazy import A->B makes A,B,V a valid nested DFS. If A already imports outside x,
and outside y imports B, merging x with y can create that route without merging
A,B,V. At least x is in the forward closure W.

### Forward-closure obstruction proof

Suppose a completion contracts no pair touching W. Every W block remains exactly
the same block, with the same entry identity and guards. W is forward-closed in
the current nonentry I graph, so no source import can create a new outgoing edge
from W to a nonentry outside block merely by contracting outside blocks. Its
internal I/C edges therefore remain identical; imports into entry also retain
identical behavior. Incoming outside edges can choose a first root of S, but the
intrinsic UNSAT certificate already excludes every such root. Outside activity
cannot provide a new return route through W because there is no outgoing edge
from W to those outside blocks. Hence the completion remains unschedulable, a
contradiction. At least one of the clause's contractions is necessary.

## Constructor and coverage

Maintain a canonical partition, domain intersections, and anchor identities.
Clone partitions for alternative contractions and reuse the prepared source
graph for each quotient projection. Propagate deterministic contractions and
contradictions first. When a necessary disjunction remains, select its dedicated
branch generator, try compatible contraction alternatives, and recursively
propagate. Deduplicate partitions by canonical block membership plus scope/domain
state. Do not invoke a generic complete-assignment checker as the branch
predicate.

A state needs a contraction when a domain, cap, C-cycle, entry or intrinsic
schedule obstruction requires one. If none does, every remaining unanchored
block can receive its own permitted fresh path, the cap holds, and the schedule
constructor supplies the witness. Evaluate positive residual gain against A0.
If there is no gain, all atoms are anchored in residual baseline owners and no
further coarsening can create gain; reject. Otherwise construct the exact batch
and independently certify the complete assignment plus retained schedule once
before reporting `found`. Certification remaps the retained schedule by logical
owner identity into the final module universe. Only unused isolated explicit
fresh-owner anchors and entry's corresponding fanout targets may disappear.
The independent verifier checks the exact required target sets, runs DFS and
checks all constraining edges; it does not repeat schedule construction.
A certification disagreement is an internal error, not a branch failure or
exhaustion proof.

Coverage follows by fixing any permitted accepted target P*. The initial
partition refines P*. At every forced contraction, the necessary-constraint
proof implies P* contains that equality. At every disjunction, at least one
alternative consists only of equalities in P*. Its domain intersections cannot
be empty because P* supplies a valid final owner. Follow that branch. Each
contraction strictly reduces the number of blocks, so this path terminates.
If it reaches a witness earlier, existence is established; otherwise it reaches
P* and must succeed. Exhausting every valid contraction branch therefore proves
there is no permitted peel. Time, work and memory limits remain incomplete.

This covers joint moves and several destinations without requiring an accepted
single-atom intermediate placement. It does not claim the finest partition is
itself in scope or accepted. It does not claim an UNSAT SCC is a structural atom.

## Cost and required evidence

The number of partitions can be Bell-number scale in the worst case. The
algorithm is not a polynomial-time theorem, nor a hardness claim. Its distinction
from Cartesian assignment probing is that every branch is justified by an
explicit necessary graph/domain/cardinality relation, propagation constructs
forced consequences, and schedules are derived from adjacency instead of a
black-box acceptance callback. Whether broad closure clauses are fast enough
requires measurement; do not describe this derivation alone as completing the
production performance obligation.

The report counts analyzed partitions, quotient projections, forced contractions,
contraction attempts, clause categories and final certifications. Optimized
profiles separately assess source preparation, SCC/reachability work, failed
schedule trials and memory; these are not all separate report counters.
For chains/fanouts with unrestricted new ownership the finest partition should
already yield maximal extraction. With one fresh owner, deterministic compatible
merges should take linear many contractions, not enumerate subsets. Fixed-entry
blockers should propagate through graph edges. More independent atoms must not
multiply full assignment certifications. Verify these properties with structural
workcount assertions, and measure larger neutral/downstream workloads separately.

Independent tiny tests enumerate raw unit assignments and raw import lists; they
must not reuse contraction clauses or scheduler pruning. Compare existence,
required-unit queries and owner caps; shrink failures. Include star impossibility,
its A->B import repair, its outside-x/y contraction repair, entry promotion,
fixed-anchor contradictions, promoted edges and forced C-SCC co-location,
existing-owner extensions, simultaneous multi-owner companions, and alternate
roots/entry routes within an SCC. Preserve current policy schedules when valid
and verify emitted Node behavior for real-source fixtures.
