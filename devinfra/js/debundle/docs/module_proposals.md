# Residual proposals with explicit import order

Supported workflow on the active explicit-order engine. The graph model and
proof are in <placement_algorithm.md>; consumer adoption and behavior acceptance
are tracked separately in the active delivery plan.

`debundle modules propose` constructs a group from the current entry residual.
Existing assignments and their shared import priority stay fixed. One fresh
owner is appended to that priority, preserving the old modules' DFS traversal.
The operation starts unseeded: users do not need to find a known movable unit.
Its soundness/completeness scope is the precise witness family in
<placement_algorithm.md>, rather than arbitrary ownership/order changes.

## Workflow

Start with a graph built from the intended source, spec and analysis settings.
Keep the same graph and module tree for inspection, proposal, check and apply.
The project's wrapper or environment must supply the matching `--modules`,
`--source-root` and `--tree-config` throughout these commands:

```bash
# Inspect the graph and residual structure.
debundle graph-summary --format text
debundle atoms --residual-only --format json > residual-atoms.json

# Construct an unseeded residual peel and inspect the outcome before taking .plan.
debundle modules propose --format json > placement.json
jq '.plan' placement.json > plan.json

# Review the full assignment, verify it, then preview and apply it.
debundle modules check --plan plan.json --format json
debundle modules apply --plan plan.json --dry-run --format json
debundle modules apply --plan plan.json --format json

# Regenerate and run the project's materialization and behavior checks.
```

An executable plan contains the full final import priority, exact ownership
changes and source/spec identity. Its complete assignment passes the shared
verifier and its selectors have been preflighted against the spec writer.
Review semantic ownership and names before writing. The preview reruns the same
selector-resolution and assignment checks as apply. After
applying, regenerate the bundle and run relevant runtime checks; graph
acceptance alone does not prove emitted behavior. Refresh the graph before the
next graph-backed edit.

Default `modules check` preflights the actual writer for ordinary and selected
ownership trees. Its `scope: executable_plan` report keeps `graph_accepted`
separate from `accepted`; writer refusal returns no plan. For graph constraints
alone, explicitly use `modules check --graph-only --batch changes.json`. That
mode reports `scope: graph_only`, accepts no saved `--plan`, and always returns
`plan: null`. A graph-only result cannot authorize selector writing.

### A graph corpus

For a multi-chunk module tree, pass the directory that contains the reports
and the tree config that maps module roots to chunks:

```bash
debundle modules propose --graphs out/reports/tree \
  --modules modules --tree-config spec_config.yaml --format json \
  > placement-by-chunk.json
```

The command reads each embedded graph header's `chunk_id`, rejects duplicate
chunk reports, and requires one report for every configured chunk. It verifies
current source provenance for every discovered graph before emitting any
report. Configured chunks are the `module_roots` values, or only
`main_chunk_id` when the config has no module roots. Graphs for other chunks
are listed as skipped; the report does not claim whole-corpus completeness
outside the configured scope.

Each supported configured chunk receives an independent constructor inventory
and, when source inputs are available, a snapshot-bound plan. Unit IDs and
destinations never cross chunks; chunk-local required-unit and destination
filters are unavailable in corpus mode. Check/apply a plan with its row's graph
path and the same tree config. Generated/catchall residual routing is reported
as unsupported by this initial constructor. An aggregate negative covers only
the supported chunks and stated family; it never treats exclusions as exhaustion.

## Scope and outcomes

The initial complete operation has one policy: preserve existing owners and
their baseline traversal, moving a joint group of whole entry-residual atoms
to one fresh owner appended last in the shared priority. It derives mandatory
companions and feasible timing windows directly from the graph. Extending an
existing owner, reshuffling extracted code and seeking different import orders
are outside this operation. Requesting unsupported policies is an error.

| Option               | Effect                                                                                  |
| -------------------- | --------------------------------------------------------------------------------------- |
| `--into MODULE`      | Choose a fresh logical destination; an existing owner is outside this policy.           |
| `--require UNIT`     | Require the result to extract a residual unit and any necessary companions; repeatable. |
| `--only-units FILE`  | Restrict movement to a JSON array of units. Included structural atoms must be complete. |
| `--tree-config FILE` | Scope a multi-chunk module tree to the graph's configured chunk.                        |

Outcomes distinguish an executable checked plan, a graph-feasibility inventory
without executable selectors, and a completed no-peel result within the stated
family. A required atom is feasible exactly when some group containing it is
permitted by that family. Inventory summaries cover all seeds in one graph pass;
they need not expand every overlapping companion closure. Interruption is
inconclusive. Input/witness/certification errors are not no-peel results, and
selector-writer failure cannot become graph exhaustion.

Graphs exported from `run --ownership-subset` record the normalized selected
claims in each chunk graph and in the program graph's provenance. The exact
subset-file bytes are fingerprinted and verified with the other graph inputs.
For a passing `inline_in_entry` selected baseline, the same propose/check/apply
workflow writes new selector claims into the selected JSON's `placements` map,
along with the appended priority in `--tree-config`. It preserves the complete
base tree, semantic premises, retained identities and existing placements.
Supply the same config, module tree and source roots used for graph verification;
the captured run context supplies the remaining source and package inputs.
Selected-view destinations in `--into`, explicit batches and plans are
chunk-local owner paths, matching the selected JSON's placement keys.

Before offering a selected-view plan, the writer synthesizes ordinary selectors
on the prepared source AST and resolves the actual staged view through the
normal materializer's ownership and structural placement rules. Its complete
source-unit assignment must equal the checked candidate. Preview repeats this
check and writes nothing; apply stages both documents before writes. Changed
base, source, config or view inputs invalidate the graph/plan. A graph-feasible
group with no realizable selectors remains `inventory_only` with its blocker.
Moving existing owners, extending a current owner, changing the baseline
priority, other selected-view edit verbs, and optional `--analysis` effect
assessment are unsupported. Never apply selected graph IDs to the base module
YAMLs or persist them as selector coordinates.

| Constructor status             | Meaning                                                                                         |
| ------------------------------ | ----------------------------------------------------------------------------------------------- |
| `found`                        | A selected group has an executable plan after graph verification and selector preflight.        |
| `inventory_only`               | Graph-feasible units were found, but selector realization did not yield an executable plan.     |
| `unsupported_residual_routing` | Residual units remain in generated/catchall owners outside the supported entry-residual family. |
| `exhausted_no_peel`            | The completed query found no peel within its advertised family and explicit movement scope.     |

`unsupported_residual_routing` also lists excluded owners and their source units.
A found plan can coexist with these exclusions. Corpus aggregation retains the
unsupported outcome when there is no executable plan or graph-only inventory;
it must not turn unsupported chunks into whole-corpus exhaustion.

SCC condensation plus max/min/blocked propagation derives feasibility in linear
graph work, with ordered-map overhead in the current implementation. Only the
selected closure is materialized and independently certified. Source analysis,
selector resolution and spec writing are measured separately. No partition
enumeration, arbitrary scheduler, or heuristic fallback follows a failed window
test. A future heuristic must have an explicit incomplete contract and opt-in.

Plans use logical module destinations and record graph and program identity.
`modules check --plan` and `modules apply --plan` verify that identity before
using the plan. For full-tree editing, `--tree-config` maps logical plan
destinations through the graph chunk's configured physical module roots.
A changed destination map or priority
needs a new complete check. Applying one plan invalidates peer plans made from
the same old snapshot: refresh the graph and propose again.
