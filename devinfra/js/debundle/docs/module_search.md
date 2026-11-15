# Exact residual placement

`debundle modules propose --strategy complete` searches for a positive-gain
placement of movable residual source units while keeping authored source-unit
ownership fixed. It can leave atoms in their baseline residual owner, join them
to an existing authored owner, or place them in generated residual owners.
Generated residual owners stay distinct from the true entry module. The search
uses structural atoms, the shared incremental graph index, and the global
canonical realizability gate; its result is a graph placement witness or a
scoped no-placement decision.

The constructive coverage and scheduler proofs live in
[module placement](module_placement.md). This guide covers the CLI and author
workflow.

## Workflow

Start from a passing graph produced with the intended spec, source, engine and
analysis assumptions. Use the same graph and module roots throughout the query
and plan review:

```bash
# Inspect the current residual and its graph context.
debundle graph-summary --format text
debundle atoms --residual-only --format json > residual-atoms.json

# Construct a complete-scope placement and retain its snapshot-bound plan.
debundle modules propose --strategy complete --format json > placement.json
jq '.plan' placement.json > plan.json

# Review ownership and any fresh destinations, then check and preview the plan.
debundle modules check --plan plan.json --format json
debundle modules apply --plan plan.json --dry-run --format json
debundle modules apply --plan plan.json --format json

# Regenerate and run the project's behavioral checks on the emitted bundle.
```

Save or apply `.plan` only when status is `found`. The plan binds dependency and
program graph digests. Graph preflight also verifies the current engine, source,
spec, assumptions and relocation roots, so refreshed unit IDs cannot retarget
an old plan silently. If the destination mapping changes, check the resulting
complete mapping again.

Review the semantic ownership and synthesized selectors before application.
`found` means the exact graph placement passed the canonical gate. Selector
resolution, materialization and runtime behavior remain separate evidence.
Preview and application resolve the complete resulting spec, require every
source-unit destination to match the plan, then run their normal full checks.
Refresh the graph after editing.

The packaged [peel-more recipe](../skills/shared/examples/peel_more.py) follows
this sequence in a worker snapshot and records each command's actual outcome.
Its archive test executes the shipped recipe and checks the resulting entry
under Node.

## Scope and outcomes

The current owner policy is fixed: authored source units stay at their authored
destinations, while movable residual body atoms may use the allowed destinations.
The controls and defaults are:

| Control                              | Meaning                                                                                                         |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `--existing-assignments fixed`       | Fixed authored source-unit ownership; this is the only supported policy and the default.                        |
| `--destinations new\|existing\|both` | Allowed destination kinds; default `both`.                                                                      |
| `--into MODULE`                      | Restrict destinations to exact module paths; repeatable. Existing owners are checked in place.                  |
| `--require UNIT`                     | Require every returned placement to extract this residual unit; necessary companions may also move. Repeatable. |
| `--only-units FILE`                  | Restrict movable units to a JSON array. Each included structural atom must be complete.                         |
| `--max-new-modules N`                | Bound the number of fresh residual owners in the final placement.                                               |
| `--max-checks N`                     | Explicit budget on analyzed partition states.                                                                   |
| `--time-budget-ms N`                 | Explicit wall-clock budget. Exhausting either budget is incomplete.                                             |

Import/export scaffolding and authored units remain in place. Atoms containing
authored units or scaffolding cannot move. Generated residual owners are
distinct from entry and cannot be claimed as new authored destinations. New
paths in the reserved `residual/` namespace are excluded. Fresh owner names are
existential for acceptance; readable canonical labels provide stable output and
do not restrict the search to lexical gaps. With `--into`, the supplied fresh
paths are the available names and each resulting fresh owner uses a distinct
path.

| Status              | Guarantee                                                                                                                                                               |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `found`             | Positive residual gain and one exact placement accepted by the full canonical graph gate; exit 0.                                                                       |
| `exhausted_no_peel` | Every compatible contraction branch in the reported permitted scope was exhausted; no positive-gain placement exists under this graph model and scope; exit 0, no plan. |
| `incomplete`        | An explicit work or time budget stopped analysis; exit 1 and no no-placement conclusion.                                                                                |

Preflight, unsupported scope, malformed input, execution and serialization errors
fail without a no-placement result. Interruption cannot become exhaustion.
`--limit` and `--size-cap-lines` remain advisory output controls and are rejected
in complete mode. The report records the move scope, work counts for partition
states, quotient projections, forced contractions, branch attempts, final
certifications and clause categories, plus residual gain and existing-unit
disturbance.

## How the search works

The search begins from a finest partition: fixed authored owner anchors and one
block per movable atom. It propagates graph-derived necessary equalities and
domain constraints, then branches only where a necessary contraction clause has
multiple compatible alternatives. Chains and fanouts of forced constraints
contract directly; the search does not enumerate the Cartesian product of every
atom's stay/target choices. It supports joint moves and placements across several
destinations without requiring accepted single-atom intermediate states.

An exact schedule is part of the placement check. The current preferred import
order is retained when it satisfies every constraining edge. Otherwise the
scheduler constructs an order from the actual import graph, committing valid
DFS subtrees and exploring alternative white successors where necessary. A
schedule obstruction contributes a necessary forward-closure contraction
clause. Once a complete placement is constructed, one full shared canonical gate
certifies it; prediction during search uses the shared graph index.

Constraint contraction reduces the number of blocks at each step, but the
necessary disjunctions can branch across partitions, with Bell-number worst
cases; exact schedule construction can also backtrack. There is no polynomial-
time guarantee. `--max-checks` limits analyzed partition states rather than an
arbitrary count of complete assignment retries; quotient projections and the
other work counts expose what happened within those states. Exhausting the
budget is `incomplete`; only full branch exhaustion establishes
`exhausted_no_peel` in the reported scope.

The default or `auto` proposal strategy remains a heuristic inventory for source
review while its migration to exact placement is pending. Its readiness labels,
diagnostics and empty output cannot establish that no peel exists.

The placement proof is a graph argument relative to the captured assumptions
and conservative checker. It is not evidence of selector stability or runtime
behavior. Tests exercise the construction and its edge cases; tests supplement
the proof but do not establish general correctness on their own.
