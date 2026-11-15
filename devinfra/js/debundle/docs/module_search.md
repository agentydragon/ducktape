# Complete residual search

`modules propose --strategy complete` searches an accepted partial assignment
with authored owners fixed. Existing owners may receive residual units. It uses
the same `AssignmentProblem` check as `modules check` and exact-plan application:
structural atoms, cross-module rebinding and the full global ESM gate, including
remaining residual code. Completeness is relative to that conservative checker,
its prepared graph and captured analysis assumptions.

## Agent workflow

Start from a successful `run --graphs` using the intended spec, engine and inputs.
Set the graph, module tree and source context consistently for every command:

```bash
debundle modules propose --strategy complete --format json > search.json
jq '.plan' search.json > plan.json
debundle modules check --plan plan.json --format json
debundle modules apply --plan plan.json --dry-run --format json
debundle modules apply --plan plan.json --format json
# Run the original materialization command again, then relevant behavior checks.
```

Only save/apply `.plan` when the search status is `found`. The plan binds the
dependency and program graph digests; the ordinary graph preflight also verifies
the current engine, source, spec, assumptions and relocation roots. Reusing unit
IDs after refreshing the graph cannot silently retarget a saved plan.

Plans contain logical module destinations. Application locates an existing
logical owner in the configured physical roots, or creates a new owner under the
first configured root. An ambiguous existing owner is an error. An explicit raw
batch uses physical destinations when `--tree-config` is supplied; `modules check`
can convert an accepted batch into a context-bound `.plan` too.

Review the chosen module ownership and selectors. `found` establishes graph
acceptance; synthesis may still lack a unique selector. Preview and application
resolve the complete resulting spec and require every source-unit destination
to match the plan. They then run the same complete checker before writing.
Refresh the graph after an edit. Runtime behavior remains separate evidence.

The packaged [peel-more recipe](../skills/shared/examples/peel_more.py) performs
this sequence in a verified worker snapshot, recording every actual exit status
and materialization result. Its archive test executes the shipped script and
checks the resulting entry under Node.

## Scope and termination

| Control                                 | Meaning                                                                                     |
| --------------------------------------- | ------------------------------------------------------------------------------------------- |
| `--existing-assignments fixed`          | The only implemented owner policy; also the default.                                        |
| `--destinations new\|existing\|both`    | Allowed destination kinds; default `both`.                                                  |
| `--into MODULE`                         | Restrict targets to exact destinations; repeatable. Checks existing owners in place.        |
| `--require UNIT`                        | Every returned peel must include this residual unit; repeatable. Companions may move.       |
| `--only-units FILE`                     | JSON array of the only units allowed to move. A cut atom is an input error with companions. |
| `--max-new-modules N`                   | Explicit bound on new owners, applied to whole assignments.                                 |
| `--max-checks N` / `--time-budget-ms N` | Explicit search budgets. No implicit budget in complete mode.                               |

Import/export scaffolding stays in its baseline destination. Atoms containing
authored units or scaffolding cannot move. Generated residual destinations remain
distinct from entry and cannot be claimed as new authored destinations. New paths
in the reserved `residual/` namespace are excluded. Invalid or contradictory
options fail instead of changing the search scope silently.

| Status              | Guarantee                                                                                                |
| ------------------- | -------------------------------------------------------------------------------------------------------- |
| `found`             | Positive residual gain and an exact full-checker-accepted plan; exit 0.                                  |
| `exhausted_no_peel` | Every permitted graph assignment has been considered through the finite representation; exit 0, no plan. |
| `incomplete`        | An explicit budget stopped search; exit 1, no impossibility claim.                                       |

Preflight, unsupported scope, malformed input, execution and serialization errors
fail without a no-peel result. Interruption cannot become exhaustion. `--limit`
and `--size-cap-lines` are rejected in complete mode: they are advisory output
controls, not restrictions on this decision problem. The report records its move
scope, checked-candidate count, residual gain and existing-unit disturbance.

The default/`auto` strategy still uses the advisory factorizer. Its readiness
labels and empty output do not carry complete-search guarantees. Use complete
mode for a decision and exact plans for the check/apply loop.

## Finite representation and coverage argument

The graph and accepted baseline are finite. Let `n` be the number of permitted
residual body atoms. Every checker-accepted assignment keeps each atom whole, so
enumeration may treat an atom as one variable. Authored units, scaffolding and
units outside the requested scope retain their exact destinations. Each variable
may stay at its original residual destination, join an allowed existing owner or
enter an allowed fresh owner. At most `n` fresh owners can be nonempty.

Fresh owner spelling cannot be treated as arbitrary symmetry: canonical module
path order determines numeric module identities and can break import-order ties.
The enumerator therefore chooses up to `n` distinct fresh representatives in
**each lexical gap between authored owners**, including before the first and after
the last. An explicit new-owner bound can reduce that count. Reserved/generated
paths are excluded. If a gap contains fewer permitted paths, every such path is
represented. Readable names are preferred; the exact fallback walks lexical
successors using U+0001, the least non-NUL filename character, and skips the
reserved subtree. Rare tight gaps can therefore produce escaped control
characters in graph-plan paths. Review renaming requires another exact check.

For any permitted concrete assignment with `k <= n` fresh owners, partition its
fresh paths by their gap among the fixed authored paths. Map the paths in each
gap, in order, to distinct representatives there. Preserve which atoms share an
owner and every residual destination. The ordered nonempty owners, their source
units and their dependencies are identical under this mapping. The checker gets
the same numeric assignment and ESM ordering, hence the same verdict. This proves
that finite representative enumeration includes a witness whenever one exists;
it does not assume an unproved global renaming symmetry.

The implementation first tries putting all permitted atoms together at each
destination as a useful cheap witness. A failed probe does not prune anything.
It then enumerates the full product of stay/target choices. It checks complete
assignments, including unions whose individual moves reject, and permits several
destinations in one witness. Required-unit and new-owner constraints filter only
assignments outside the declared scope. No heuristic closure, accepted-only
search path, pairwise seed reconstruction or candidate-size bound narrows the
fallback. The product is finite; without an explicit budget it eventually returns
a witness or exhausts it. It can be exponential and makes no optimality promise.

## Regression and property coverage

- `modules_assignment_oracle_test` enumerates 64 raw statement assignments
  independently of atoms/planner contractions. Each concrete spec runs through
  normal selector resolution and materialization; accepted output executes under
  Node. Checker decisions must agree.
- `modules_complete_proptest` generates small eager/lazy programs and permitted
  unit/destination scopes, exhaustively materializes their raw assignments, then
  compares existence with the complete CLI result. Every returned witness goes
  through check, preview, apply and Node. This generator uses distinguishable
  declaration literals so selector realization is possible. Proptest shrinks
  source shape and scope; Bazel uploads failing seeds and retained fixture inputs.
  Increase `PROPTEST_CASES` for longer runs and turn any shrunk failure into a
  named regression before fixing it.
- `modules_complete_cli_test` pins joint peels, actual existing owners, intact
  three-unit atoms, honest negatives/budgets (including time spent skipping
  out-of-scope combinations), scope errors, stale plan IDs and physical tree
  roots. `modules_check_cli_test` also distinguishes a valid
  asymmetric import cycle from a Pass-2 ESM rejection with no constraining cycle.
- `modules_assignments_apply_test` pins anonymous/multiple-destination claims,
  exact unchanged-unit identity, metadata, empty owner shells, generated residual
  destinations and no writes on graph, selector or freshness failure, including
  the duplicate-literal binding case shrunk from the property suite.
- Pure combinatorial tests cover dense/finite path gaps and order-preserving
  representatives. Existing quotient tests retain the constructed remote-TDZ
  counterexample against local-only checks; that is distinct from the real-source
  CLI ESM cases above.

Tests support the coverage argument; finite examples alone are not a proof of
general completeness or sound JavaScript analysis.
