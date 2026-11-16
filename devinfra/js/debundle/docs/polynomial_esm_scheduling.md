# ESM schedule feasibility and bounded construction

Deciding whether arbitrary canonical import lists can be ordered to satisfy
finish constraints is NP-complete. Checking supplied ordered lists is
polynomial. The proof below explains the boundary and why SCC decomposition or
successful-subtree commitment does not remove it.

The explicit-order contract verifies the priority supplied with the spec; its
complete residual constructor uses the restricted family proved in
<placement_algorithm.md>. It does not call the optional bounded constructor
described below. That separate algorithm can return a verified schedule, a
proved obstruction or `unknown`; failure to find an order is not impossibility.

## Problem and scope

Let `V` be the emitted modules, including distinguished entry `e`. Let `I` be the
canonical directed import edges and `C ⊆ I` the constraining edges. An edge
`u → v` in `C` requires the body of `v` to finish before the body of `u` starts
running, represented by `finish(v) < finish(u)` in the synchronous ESM evaluation
model. Entry imports every other emitted module. Every canonical import must
remain present exactly once. The constructor may reorder import lists; it may
not invent imports, remove constraints, merge authored modules, or assume effects
commute.

A candidate schedule is one ordered adjacency list per module. Evaluation is
ordinary DFS from entry: enter a white vertex, mark it gray, visit its ordered
imports, then mark it black and assign its finish index. A gray target is an
ancestor; a black target has already finished. All modules must be reached.

This problem is exactly the graph-level scheduling obligation in
[module placement](polynomial_esm_scheduling.md#why-successful-subtree-commitment-does-not-bound-retries). It does
not establish the semantic sufficiency of the supplied `C` edges. That remains
the responsibility of whole-program effect and availability inference and the
other gate checks. Async modules and arbitrary alternate entry roots require
separate models.

The polynomial bounds below start with the explicit canonical graphs. They do
not assert that selector matching, CP-SAT selector resolution, complete module
placement search, or the entire CLI is polynomial. None of those algorithms is
called by this constructor.

## Exact feasibility is NP-complete

Reduce directed Hamiltonian path on `H = (X, A)` to schedule feasibility. Add a
hub `z` and entry `e`, and define:

```text
V = X ∪ {z, e}
I = A ∪ {x → z : x ∈ X} ∪ {z → x : x ∈ X}
      ∪ {e → v : v ∈ X ∪ {z}}
C = {x → z : x ∈ X}
```

Construction takes `O(|X| + |A|)` time and space. All constraints are real import
edges. The nonentry import graph is one SCC. The constraining graph is an acyclic
star.

**Hamiltonian path implies valid schedule.** If `x₁, …, xₙ` is a Hamiltonian path
of `H`, order imports to enter `e, x₁, …, xₙ, z` before any other recursive
descent. At `z`, every `x` is gray, so its imports recurse nowhere. Thus `z`
finishes before every `x`, satisfying every constraint. The remaining imports
visit gray or black vertices.

**Valid schedule implies Hamiltonian path.** Consider the moment DFS enters `z`.
No `x` can already be black: that would make `x` finish before `z`, violating
`x → z`. No `x` can be white: `z → x` forces it to be discovered while `z` is
gray and finish before `z`, also violating `x → z`. Therefore every `x` is gray.
Gray vertices form the current ancestor stack, so the stack is
`e, x₁, …, xₙ, z`. Every consecutive `xᵢ → xᵢ₊₁` must belong to `A`, since
every added nonentry edge has `z` as an endpoint. These edges form a Hamiltonian
path of `H`.

Ordered import lists are a polynomial-size certificate. After checking that
each list is a permutation of its canonical targets, a DFS and one scan of `C`
verify the certificate in polynomial time. Hence feasibility belongs to NP;
the reduction proves NP-hardness and therefore NP-completeness.

The underlying hardness fact is directed Hamiltonian path. For completeness,
reduce directed Hamiltonian circuit to path by splitting one vertex `v` into
`s` and `t`, sending `v`'s outgoing edges from `s` and its incoming edges to `t`.
There are no edges into `s` or out of `t`, so a Hamiltonian path must start at `s`
and end at `t`; joining them recovers the circuit. Directed Hamiltonian circuit
is one of the complete problems in
[Karp's original paper, reprinted here](https://www.cs.umd.edu/~gasarch/BLOGPAPERS/Karp.pdf).
Use loop-free graphs with at least two vertices for this reduction.

Consequently, a general exact polynomial scheduler would imply `P = NP`. A
polynomial design must expose incompleteness, restrict its supported inputs, or
change the emission problem. The optional construction below exposes incompleteness.

### Exact trial completeness

A recursive exact constructor can commit successful DFS subtrees without
backtracking over their sibling order. This proves completeness, not a
polynomial running-time bound. Let R be the vertices reachable from a candidate
v in the current white-induced import graph. Reject a trial if a vertex in R
has a C dependency on v or an active ancestor; those dependencies cannot finish
first. Subject to that entry check:

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
completion exists, its next white child has a valid subtree and its completion
trial will succeed. Trying other children first either fails without state change or
commits a successful subtree, which preserves existence by the lemma. Each
success reduces the white set. Consequently failure after all currently possible
children fail is exact, and there is no need to backtrack over previously
successful sibling subtrees. The returned target lists are still checked by the
independent schedule verifier.

### Why successful-subtree commitment does not bound retries

Take `H` to be a complete directed graph on `k` vertices plus one isolated vertex
`q`, then apply the reduction. There is no Hamiltonian path. In a trial rooted
at a clique vertex, `z` cannot be entered successfully while `q` is white, and
no clique vertex can finish before `z`. A recursive constructor that retries
every failing white child therefore explores all orders of the remaining
`k − 1` clique vertices: at least `(k − 1)!` failed paths for that root. No
successful sibling can be committed to remove these retries. SCC decomposition
and an acyclic-`C` precheck both leave this example intact.

This is a lower bound for the uncached recursive trial scheme, not a
proof that every possible exact algorithm has factorial running time. A bounded
failure cache or faster rollback does not by itself establish a polynomial bound.

## Result contract

```text
Scheduled { ordered_imports, finish_indices }
ProvedImpossible { constraining_cycle | constraint_into_entry }
Unknown { blocked_frontier, active_stack }
```

`Scheduled` certifies the exact supplied target sets and all supplied constraints.
`ProvedImpossible` carries an independently checkable certificate. `Unknown`
means this construction did not find a schedule; another import ordering may
work. It cannot become an intrinsic SCC obstruction, an atomicity assertion,
or `exhausted_no_peel`.

## Construction

Use dense module IDs, forward and reverse adjacency arrays, color arrays, and
an explicit stack. Preserve the already supplied stable target preference; no
solver or search over permutations is needed.

1. Validate endpoints, entry fanout, and `C ⊆ I`. Malformed inputs are errors.
2. Find a directed cycle in `C`, including self loops, or a constraining edge
   from a nonentry module into entry. Return its `ProvedImpossible` certificate.
3. Simulate the preferred import lists and verify them. If they satisfy every
   constraint, return `Scheduled` immediately.
4. Otherwise run the monotone construction below. It never rolls back a push or
   retries a failed descendant under another ancestor stack.

At any point let `W`, `A`, and `B` be the white, gray, and black vertices.
Define:

```text
seeds(W, A) = {w ∈ W : there is (w → a) ∈ C with a ∈ A}
bad(W, A)   = {v ∈ W : v reaches a seed in I[W]}
```

Reachability includes paths of length zero. Compute `bad` by one reverse graph
walk from the seeds, following only edges with both endpoints white. Entering
such a vertex would force a white consumer to finish before one of its gray
prerequisites. Recompute `bad` after a push or pop; never retain it across a
color change without recomputation.

```text
mark entry gray; stack = [entry]
tree_children[u] = [] for every module u

while stack is nonempty:
    r = stack.last
    bad = reverse_reachable(seeds(W, A), I[W])
    pending = white canonical import targets of r, in stable preference order

    if pending is empty:
        mark r black; pop r; assign its next finish index
    else if some v in pending is outside bad:
        choose the first such v
        append v to tree_children[r]
        mark v gray; push v
    else:
        return Unknown {pending, stack}

for each u:
    imports[u] = tree_children[u] in recorded order
                 followed by all remaining canonical targets in stable order

independently verify target permutations, reachability, and all C finish inequalities
return Scheduled only if verification succeeds
```

An unexpected verification failure is an implementation error, not a reason to
emit output or claim impossibility. Tree-child membership uses per-edge flags,
not a linear `contains` inside the final edge loop.

## Correctness

**Lemma 1: rejecting a `bad` child is justified for the current prefix.** If
`v ∈ bad`, a white path leads from `v` to a white `w` with `w → a ∈ C` and
gray ancestor `a`. While a call on `v` is active, the vertices initially white
and reachable from `v` must all be discovered and finish before that call
returns. None can finish after `a`, which remains an ancestor. Therefore no
valid completion of that child call exists under this prefix. This does not
prove that the prefix, a different root choice, or the whole assignment is
intrinsically impossible.

The diagnostic can retain reverse-walk parent pointers and one offending active
target per seed. These share paths across the blocked frontier and keep the
certificate within `O(N)` space; do not copy a full path separately per target.

**Lemma 2: no entered module has a constraining edge to an existing ancestor.**
Entry is handled by the input checks. Every subsequent pushed vertex is outside
`bad`. In particular it is not a seed, so it has no constraining edge to any
already gray vertex. While this vertex remains on the stack, its ancestors do
not change. Self constraints have already been rejected as cycles.

**Lemma 3: every finished module satisfies its outgoing constraints.** A vertex
is popped only when it has no white import targets. Its constraining targets
are also import targets. They cannot be gray by Lemma 2, and cannot be itself
because self constraints were rejected. Hence they are all black and have
strictly smaller finish indices.

**Lemma 4: the returned lists reproduce the recorded DFS.** Every recorded child
was white when pushed. Traverse these children first, in their recorded order.
By induction each child reproduces its recorded subtree and color changes.
When the last child returns, all remaining import targets are gray or black:
otherwise the construction would not have popped this module. Appending those
imports therefore causes no extra recursive calls. The same holds at entry,
whose fanout guarantees that all modules finish. Thus the emitted adjacency
lists produce the recorded finish order.

**Theorem: accepted schedules are sound.** Lemmas 3 and 4 establish every required
finish inequality for the actual returned import lists. The independent final
verifier also checks that no canonical target disappeared or was invented.
No semantic assumption is weakened to obtain acceptance.

**Theorem: negative certificates are sound.** A `C` cycle would require a strict
finish inequality cycle, which is impossible. Entry finishes last because it
reaches every module, so a nonentry constraint into entry is also impossible.
These proofs do not use the greedy constructor or its failures.

**Termination.** Every successful loop iteration either pushes one previously
white vertex or permanently pops one gray vertex to black. Each vertex is
pushed and popped at most once. An unsuccessful iteration returns `Unknown`.
There is no recursive retry tree or fixed point with an unbounded number of
updates.

## Time and space proof

Let `n = |V|`, `m = |I|`, `c = |C|`, and `N = n + m + c`. Use explicit simple
graphs and word-sized dense IDs. Canonicalization and input-byte processing are
separate preprocessing costs; their deterministic sorting bound must be counted
if callers supply unsorted strings instead of indexed adjacency.

Input checks, cycle detection, preferred DFS, and each independent verification
take `O(N)` with indexed adjacency and per-edge marks. One monotone iteration
scans at most `c` constraints for seeds, `n + m` items in the reverse walk, and
one adjacency list of at most `m` targets. Thus it costs `O(N)`.

There are at most `n` pushes and `n` pops, plus one possible terminating failed
iteration. Consequently the full construction takes `O(nN)` time, at most
`O(n³)` for simple graphs. This bound holds for accepted, impossible, and unknown
inputs, including the factorial family above. A constant number of additional
fixed preference orders retains the bound; an input-dependent unbounded retry
portfolio does not.

Forward/reverse graphs, constraints, colors, stack, reverse-walk queue, visited
marks, and recorded child lists occupy `O(N)` space in total. Reuse the reverse
walk buffers; do not store a snapshot per stack frame. On a bit-cost model,
account for `O(log n)`-bit identifiers in these bounds.

## Supported completeness and deliberate limitations

Every already-valid preferred schedule is accepted in `O(N)` time. If `C` is
empty, the preferred schedule is always valid.

If the nonentry import graph is acyclic and there is no constraint into entry,
the construction is complete. A white candidate reaching a seed with a
constraint into a nonentry ancestor would form an import cycle through the
active stack, contradicting acyclicity. Entry cannot be that target by
precondition. Hence no white candidate is blocked, and construction finishes.

In cyclic graphs, `Unknown` can occur for a valid input. Apply the Hamiltonian
construction to `H = ({a,b}, {a → b})`. If the preference starts with `b`, the
monotone constructor cannot finish `z` while `a` remains white, and returns
`Unknown`. The alternative `entry, a, b, z` is valid. This is a documented
precision limit, not an incorrect negative certificate.

Conservative value havoc cannot make a missing valid schedule into a proven
invalid one. Adding conservative effect edges only strengthens obligations;
it cannot provide the absent scheduling witness. An author may revise module
boundaries and rerun validation, but this constructor does not silently change
the authored assignment.

## Boundary and validation obligations

The bounded algorithm above is a separate mathematical construction, not the
ordinary materialization policy. Adopting it as a named heuristic would require
typed verified/obstructed/unknown results and explicit caller opt-in. It cannot
silently repair a rejected supplied priority or restore unrestricted placement
completeness. The initial supported residual operation uses the explicit-order
family and proof in <placement_algorithm.md>.

Independent raw import-list permutations on tiny graphs can check acceptance
and distinguish a genuine obstruction from a valid-but-unfound schedule. Useful
regressions include the hub reduction, the clique-plus-isolated-vertex retry
family, gray ancestors cutting white reachability, and acyclic C inside a cyclic
I component. A deterministic operation bound must count graph work, including
failed trials; a timeout or a finite passing corpus is not a complexity proof.

These graph bounds exclude selector resolution and source analysis. A supplied
schedule must still pass the shared verifier and the remaining semantic gates
before emission.
