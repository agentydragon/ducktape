# Exact residual placement

`debundle modules propose` searches by default for a positive-gain placement of
movable residual source units while keeping authored source-unit ownership
fixed. Atoms may remain residual under the configured policy, join an existing
authored owner, or move to a fresh authored/extracted owner. Fresh extracted
owners are distinct from generated residual layout and true entry. The search
uses structural atoms and graph-derived constraints, then certifies a constructed
placement with the global canonical realizability gate. Its result is a graph
placement witness or a scoped no-placement decision. `modules propose` always
runs this exact search; `--strategy` is not supported.

The [scheduling complexity proof](polynomial_esm_scheduling.md) explains the
cost of the current general operation. The
[explicit-order graph model](placement_algorithm.md) defines the scoped target
constructor and its guarantee; it does not establish that this CLI already
implements that replacement. This guide covers the current CLI and workflow.

## Workflow

Start from a passing graph produced with the intended spec, source, engine and
analysis assumptions. Use the same graph and module roots throughout the query
and plan review:

```bash
# Inspect the current residual and its graph context.
debundle graph-summary --format text
debundle atoms --residual-only --format json > residual-atoms.json

# Construct a complete-scope placement and retain its snapshot-bound plan.
debundle modules propose --format json > placement.json
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
Preview and application resolve the complete authored-claim map and compare it
with the starting graph's claims overlaid with the plan's logical destination
changes. Omitted authored claims stay in place; unclaimed units continue through
the configured residual policy. Generated `PerAtomicGroup` filenames may
renumber when that residual layout is regenerated, so they are not stable plan
identities. The commands then run their normal full checks. Refresh the graph
after editing.

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
| `--destinations new\|existing\|both` | Allowed destination kinds; default `both`.                                                                      |
| `--into MODULE`                      | Restrict destinations to exact module paths; repeatable. Existing owners are checked in place.                  |
| `--require UNIT`                     | Require every returned placement to extract this residual unit; necessary companions may also move. Repeatable. |
| `--only-units FILE`                  | Restrict movable units to a JSON array. Each included structural atom must be complete.                         |
| `--max-new-modules N`                | Bound the number of fresh authored/extracted owners in the final placement.                                     |
| `--max-checks N`                     | Explicit budget on analyzed partition states.                                                                   |
| `--time-budget-ms N`                 | Cooperative placement deadline after graph/baseline preparation. Exhausting either budget is incomplete.        |

Import/export scaffolding and authored units remain in place. Atoms containing
authored units or scaffolding cannot move. Generated residual owners are
distinct from entry and cannot be claimed as new authored destinations. New
paths in the reserved `residual/` namespace are excluded. Fresh owner names are
existential for acceptance; readable canonical labels make a proposal
deterministic without restricting the search to lexical gaps. With `--into`,
the supplied fresh paths are the available names and each resulting fresh owner
uses a distinct path.

| Status              | Guarantee                                                                                                                                                               |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `found`             | Positive residual gain and one exact placement accepted by the full canonical graph gate; exit 0.                                                                       |
| `exhausted_no_peel` | Every compatible contraction branch in the reported permitted scope was exhausted; no positive-gain placement exists under this graph model and scope; exit 0, no plan. |
| `incomplete`        | An explicit work or time budget stopped analysis; exit 1 and no no-placement conclusion.                                                                                |

Preflight, unsupported scope, malformed input, execution and serialization errors
fail without a no-placement result. Interruption cannot become exhaustion.
The report records the move scope, work counts for partition
states, quotient projections, cooperative schedule checkpoints, forced
contractions, branch attempts, final certifications and clause categories, plus residual gain and existing-unit
disturbance.

## How the search works

The search begins from a finest partition: fixed authored owner anchors and one
block per movable atom. It propagates graph-derived necessary equalities and
domain constraints, then branches only where a necessary contraction clause has
multiple compatible alternatives. Chains and fanouts of forced constraints
contract directly; the search does not enumerate the Cartesian product of every
atom's stay/target choices. It supports joint moves and placements across several
destinations without requiring accepted single-atom intermediate states. At
each analyzed partition, the search rebuilds quotient projections from the
prepared edge set to derive graph constraints. It runs the full canonical gate
once after constructing a complete placement; it does not maintain the batch
incremental index through every contraction.

An exact schedule is part of the placement check. The current preferred import
order is retained when it satisfies every constraining edge. Otherwise the
scheduler constructs an order from the actual import graph, committing valid
DFS subtrees and exploring alternative white successors where necessary. A
schedule obstruction contributes a necessary forward-closure contraction
clause. Once a complete placement is constructed, one full canonical gate
certifies it.

Constraint contraction reduces the number of blocks at each step, but the
necessary disjunctions can branch across partitions, with Bell-number worst
cases; exact schedule construction can also backtrack. There is no polynomial-
time guarantee. `--max-checks` limits analyzed partition states rather than an
arbitrary count of complete assignment retries; quotient projections and the
other work counts expose what happened within those states. Exhausting the
budget is `incomplete`; only full branch exhaustion establishes
`exhausted_no_peel` in the reported scope.

The time budget is polled inside schedule derivation as well as between placement
steps. An interrupted schedule trial is neither an obstruction nor a proof of
exhaustion. Individual graph construction, cloning and verification operations
may finish before the next poll, so this is a cooperative deadline rather than a
hard process timeout. Graph loading and accepted-baseline preparation precede it.

The placement proof is a graph argument relative to the captured assumptions
and conservative checker. It is not evidence of selector stability or runtime
behavior. Tests exercise the construction and its edge cases; tests supplement
the proof but do not establish general correctness on their own.
