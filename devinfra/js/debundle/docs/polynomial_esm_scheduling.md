# Polynomial ESM schedule construction

Status: proposed design, not the behavior of the current exact constructor.

Ordinary validation and emission should use a polynomial, sound, incomplete
constructor. It returns a verified schedule, a proved obstruction, or `unknown`.
An `unknown` result refuses runnable emission and remains distinguishable from a
proof that no schedule exists. This fits Debundle's soundness-over-completeness
contract. It does change the current scheduler's completeness contract.

There is a fundamental reason for this distinction: deciding whether an arbitrary
canonical import graph admits a valid schedule is NP-complete. The reduction below
uses an acyclic constraining graph, so checking constraining cycles first does not
remove the difficulty. This document gives both that proof and a polynomial
construction with explicit correctness and space/time bounds.

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
[module placement](module_placement.md#exact-scheduler-with-successful-subtree-commitment). It does
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
change the emission problem. This design exposes incompleteness.

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

## Integration and behavioral coverage

This is a replacement design for ordinary scheduling, not an implemented repair
and not a claim that the current Excalidraw CPU profile is caused by scheduling.
That profile is dominated by callback promotion during analysis.

Implementation must replace ordinary constructor calls in `assignment_analysis`,
`realizability`, its incremental simulator, and emission-facing prediction.
Carry `Unknown` through typed results to a distinct CLI refusal. Existing
`unwrap_or_else` policy fallbacks must not erase it. The legacy complete placement
search needs an explicit decision: remain a separately named exponential mode
outside polynomial runs, or propagate unknown and forgo exhaustive/no-peel
claims. It cannot retain both guarantees unchanged.

Required real-CLI tests, using bundle + spec as input:

- Preserve accepted preferred schedules, emitted target sets, module exports,
  and original/emitted Node output.
- Accept a nonentry DAG and a cyclic graph whose preferred order fails but the
  monotone constructor succeeds; execute the emitted modules.
- Refuse a genuine constraining cycle and a constraint into entry with their
  checkable certificates; keep their error kind separate from `Unknown`.
- Refuse the valid-but-unfound `a → b` hub example as `Unknown`, with no runnable
  output. A supplied valid schedule for the same input must independently pass
  verification, proving the refusal is incomplete rather than impossible.
- Run increasing instances of the clique-plus-isolated-vertex family. Assert a
  deterministic operation-count bound `K·nN`, using graph-operation counters in
  focused pure-algorithm tests, with real CLI refusal and output checks as the
  external contract. Use no wall-clock pass/fail threshold.
- Preserve direct/saved analysis parity, incremental/from-scratch gate parity,
  and complete placement interruption semantics. Neither an interrupted run nor
  `Unknown` may establish `exhausted_no_peel`.

Tiny raw-permutation oracles may remain separate fixed-size test oracles. They
are not part of ordinary validation or the polynomial construction. Before
claiming a polynomial CLI, separately resolve the selector and placement-search
boundaries and prove bounds for the remaining inference and reporting passes.
