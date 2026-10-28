# Debundle Architecture Review

Use this guide to review whether an in-progress spec and its emitted JavaScript
form a coherent application tree. The current module names and paths are useful
leads, not ground truth: infer architecture from source behavior, call sites,
graph edges, source proximity, ownership, and repeated internal structure.

## Inputs, scope, and outputs

Ask the project adapter for artifacts from the same spec, source, and engine
revision:

- the active `modules/**/*.yaml` tree and its emitted JavaScript tree;
- the matching `dependency_graph.json` and, when available, `reports/tree/`;
- project convention and taxonomy documents;
- the paths for current architecture notes and worker-ready reorganization
  recommendations.

Use the adapter's read-only graph/source queries to inspect evidence. A graph
or report from a different input revision cannot support a current
recommendation. If a needed artifact is unavailable, state the visibility
limit instead of guessing.

The architect updates the current-state architecture notes and proposes
concrete work for workers. Do not edit module YAML, run extraction gates, or
regenerate emitted JavaScript. Do not read large minified residual bodies; ask
intake to ground them. Route pure symbol naming to the [naming task
guide](naming.md). Use the [shared workflow](../skills/shared/workflow.md) when
this review is part of a coordinated round.

## Review sequence

### 1. Read the whole tree

Inspect both the spec tree and emitted tree. Read the complete active module
name list and its path segments, leaf names, exports, and importer neighborhoods
before making naming, tiny-module, or one-callable recommendations. Do not
substitute a few familiar prefixes. Then inspect source bodies and graph
neighborhoods behind suspicious paths; the tree alone does not establish an
architecture.

Look for:

- tiny modules that are implementation details of one meaningful consumer;
- modules named after a callable or implementation step instead of a durable
  concept;
- unrelated behavior glued into one module, or helpers/configuration separated
  from their real owner;
- unsupported layer direction, inconsistent depth, or competing roots for the
  same concept family;
- generic buckets such as `utils`, `helpers`, `core`, `common`, `runtime`, or
  `misc` without a project convention that explains them;
- singleton directory wrappers, overloaded sibling lists, and repeated
  `foo/foo.js` leaves with no useful namespace meaning;
- source behavior that contradicts the assigned name or path, and extracted
  modules unreachable from the generated graph.

Use the shared [module-shape guidance](../skills/shared/module_shape.md) for
seam quality, tiny-module folds, and layer ownership. Treat one-callable modules
as a recurring smell when the module merely repeats a callable's name. Keep a
standalone callable when it is a public API, a shared domain primitive, or has
multiple meaningful consumers. Otherwise follow its behavior one layer higher
to find the owning component, service, state, or parser, and propose a merge or
co-peel with that owner.

### 2. Audit directory boundaries

Review hierarchy as a system, not only as isolated files. If the adapter
provides directory reports, inspect `reports/tree/<emitted-dir>/index.json`
before recommending hierarchy changes. Incoming/outgoing symbol, file, and
edge-kind attribution shows which crossings make a directory leaky or which
concepts dominate its boundary. Read the source and graph behind the highest
attribution entries before writing a recommendation.

A directory with one child is not automatically wrong: it may name a stable
namespace, route/package, public API, or evident family. A broad directory is
not automatically wrong either: a small cohesive subsystem may have a useful
flat surface. In both cases, record the local reason. When repeated bad shapes
show a missing convention, update the project convention or architecture notes
so future peels can follow it.

### 3. Review selector health

Use the current `debundle spec selector-debt --modules <modules-dir>
--group-module-depth N` report as a diagnostic view when it is available. It
can group fragile name-only selectors and identify verbatim repeated
`source_match` bodies that may need one contextual selector. Pair the modules
tree with its matching source when using source-aware diagnostics. These are
worker findings, not architecture decisions: route coherent buckets for
selector work, and route repeated unsupported or over-verbose selector shapes
to Ducktape tooling. Follow [`selectors.md`](selectors.md) for selector
portability. Do not infer that a short name is wrong solely from its spelling.

### 4. Record evidence as scoped conventions

Move from evidence to durable guidance in this order:

1. Gather source/graph facts: edges, call sites, source proximity, import
   direction, directory pressure, sibling patterns, and well-shaped examples.
2. Record a hypothesis with its scope, supporting evidence, counterexamples,
   and open questions in `architecture-notes`.
3. Promote it to `module-reorg` only when a worker can apply the change
   without re-deciding the design.
4. Promote a durable convention into project documentation when it affects
   future edits; revise or delete a hypothesis when new evidence contradicts it.

Architecture notes describe the best current inference about the app's
architecture, not whatever the spec currently calls a module. Rewrite stale
sections in place; Git preserves history.

## Structural conformance metrics

Whenever the notes assert a desired layer or dependency relationship, pair it
with a rerunnable metric and run the target's metrics on every review. Label
each expectation as one of these:

- **RULE:** a boundary that should hold; any violation is a finding to route to
  a worker.
- **OBSERVATORY:** a hypothesis to track without failing; report its current
  value and drift from a recorded baseline.

An observatory that stabilizes may become a rule. A rule repeatedly violated
for sound reasons may reveal a bad assumption; revise the rule and notes rather
than normalizing unexplained failures.

Build metrics from stable debundler side outputs, not ad hoc parsing of emitted
imports. `dependency_graph.json` exposes `module_graph.nodes` with each module's
report path and `module_graph.edges` with typed `edge_kinds` and
`constrains_init_order`. Use `describe`, `cluster`, `scc`, `coverage`, or
`graph-summary` output when a metric needs that information. The entry uses the
synthetic `anon_residual_sentinel` report path. Project-specific scripts and
expectations belong in the project tree; keep only the generic method here.

Each check should have one named expectation and emit its measured value,
verdict, and a bounded list of concrete violating edges or modules. RULE
failures should exit nonzero so the metric can serve as a gate; observatories
report drift but never fail. Git-track small baselines next to the script and
update them deliberately after an intentional structural shift.

Examples below are shapes to adapt, not default project policy:

- **RULE, intra-app graph:** if a target's `shared/` layer must remain
  framework-agnostic, report edges from `shared/` into `domains/`, `features/`,
  or `app/`. A violation may mean a module is misplaced or its shared behavior
  should move lower.
- **RULE, vendor imports:** when `domains/` must remain React-free and React is
  supplied through a vendor swap (so the module graph cannot see it), scan the
  emitted JS for React imports under that layer. Exempt documented immovable
  fused modules so the rule measures fixable work.
- **OBSERVATORY, layer drift:** track the fraction of edges pointing up a
  proposed layer stack, or the share of `X -> app` edges reaching a registry
  subtree. Record the baseline and value; a rising share may indicate that the
  app layer is becoming overloaded, but does not yet assert a violation.
- **OBSERVATORY beside a rule:** alongside a rule against raw React imports,
  track ambiguous reactivity wrappers such as
  `framework-react/observer` separately. This can reveal whether the wrapper
  becomes a leak path without incorrectly treating every use as forbidden.

On every architect pass, run the project's conformance scripts before writing
recommendations. A newly failing rule is either a regression to route or an
assumption to revise, with the reason recorded. Drift in an observatory should
prompt a decision to intervene, promote the hypothesis to a rule, or revise
it. Any new or sharpened boundary in architecture notes needs a matching
metric in the same pass.

## Co-consumption and architecture ownership

Co-consumption helps identify local helpers, but it does not override layer
ownership. Do not move domain, policy, persistence, integration, or
infrastructure behavior into presentation code merely because that code is the
only consumer. Examples to consider as hypotheses, not universal rules:

- component-local presentation helpers or styling may belong with their one
  component consumer;
- reducers, action constants, and selectors may share a state module when they
  share a public contract;
- parser tables and token predicates may belong with a parser when they are
  implementation details;
- command metadata may belong with a handler only when it is not an independent
  registry or policy.

State the exception boundary for every inferred convention. For seam and layer
criteria, see [module shape](../skills/shared/module_shape.md).

## Outputs

`architecture-notes` is a current-state document containing observations,
scoped conventions and exceptions, suspected layer boundaries, areas needing
more evidence, and questions for intake or workers. Every asserted or tracked
boundary points to its RULE or OBSERVATORY metric. Use the dependency graph as
a constraint signal, then ground ownership claims in source and call sites;
workers establish whether a proposed move passes the extraction gate.

`module-reorg` contains only worker-ready proposals. Use a compact entry such
as:

```md
## <one-line change>

**Files involved**:

- `<modules-dir>/path/to/module.yaml`

**Evidence**:

- <source and graph evidence, plus relevant directory-shape evidence>

**Proposed change**:

- <concrete move or merge>

**Confidence**: high | medium | low
**Blocked by**: <open dependency, or none>
**Status**: proposed | dispatched | rejected
```

Delete landed proposals on the next pass. Keep a rejected item only briefly
when it prevents repeating the same mistake. Do not promote illustrative
framework examples into project policy.

## Audit snippets

These exploratory commands assume the adapter supplied `EMITTED_JS_ROOT`,
`REPORT_TREE`, and `GRAPH`. Use them to locate candidates, then inspect the
source and graph evidence; counts do not prove a bad boundary.

### Tree inventory

```sh
# Count files by top-level emitted root.
find "$EMITTED_JS_ROOT" -type f |
  sed "s#^$EMITTED_JS_ROOT/##" |
  awk -F/ '{print $1}' |
  sort | uniq -c | sort -nr

# Find singleton directories; inspect each before proposing a flatten.
find "$EMITTED_JS_ROOT" -type d |
  while read -r d; do
    n=$(find "$d" -mindepth 1 -maxdepth 1 | wc -l)
    [ "$n" -eq 1 ] && printf '%s\n' "$d"
  done |
  sed "s#^$EMITTED_JS_ROOT/##"

# Find repeated directory/file leaves such as foo/foo.js.
find "$EMITTED_JS_ROOT" -type f |
  sed "s#^$EMITTED_JS_ROOT/##" |
  awk -F/ 'NF >= 2 {
    leaf=$NF; sub(/\.js$/, "", leaf); parent=$(NF-1);
    if (leaf == parent) print $0
  }'

# Count component-style leaves that may belong with one owner.
find "$EMITTED_JS_ROOT" -type f -name styles.js | wc -l

# Read the complete module list before naming or tiny-module recommendations.
find "$EMITTED_JS_ROOT" -type f -name '*.js' |
  sed "s#^$EMITTED_JS_ROOT/##; s#\.js$##" |
  sort

# Group repeated basenames.
find "$EMITTED_JS_ROOT" -type f -name '*.js' |
  sed "s#^$EMITTED_JS_ROOT/##; s#\.js$##" |
  awk -F/ '{leaf=$NF; count[leaf]++; paths[leaf]=paths[leaf] "\n  " $0}
    END {for (leaf in count) if (count[leaf] > 1) print count[leaf], leaf paths[leaf]}' |
  sort -nr

# Find small files; combine with importer and source-proximity evidence.
find "$EMITTED_JS_ROOT" -type f -name '*.js' |
  while read -r f; do
    lines=$(wc -l < "$f")
    [ "$lines" -le 25 ] && printf '%s\t%s\n' "$lines" "${f#$EMITTED_JS_ROOT/}"
  done |
  sort -n | head -100
```

### Directory report and graph pressure

```sh
# Directories with the most outgoing symbol pressure.
find "$REPORT_TREE" -name index.json -print0 |
  xargs -0 -n1 jq -r '
    select(.path != "") |
    [.path, (.outgoing.symbols | length), (.outgoing.files | length), .outgoing.edge_count] | @tsv
  ' |
  sort -k2,2nr | head -50

# Most attributed outgoing symbols for one suspicious directory.
jq -r '.outgoing.symbols | to_entries | sort_by(.value) | reverse[] |
  "\(.value)\t\(.key)"' "$REPORT_TREE/<emitted-dir>/index.json" | head -50

# Count module dependencies between top-level report-path buckets.
jq -r '
  (.module_graph.nodes | map({key:.key,value:.path}) | from_entries) as $d |
  reduce .module_graph.edges[] as $e ({};
    ($d[$e.source] // error("unknown module source: \($e.source)")) as $s |
    ($d[$e.target] // error("unknown module target: \($e.target)")) as $t |
    ($s|split("/")[0]) as $sr |
    ($t|split("/")[0]) as $tr |
    .["\($sr) -> \($tr)"] += 1
  ) | to_entries | sort_by(.value) | reverse | .[:80][] |
  "\(.value)\t\(.key)"
' "$GRAPH"

# Count module dependencies between two-segment report-path buckets.
jq -r '
  (.module_graph.nodes | map({key:.key,value:.path}) | from_entries) as $d |
  reduce .module_graph.edges[] as $e ({};
    ($d[$e.source] // error("unknown module source: \($e.source)")) as $s |
    ($d[$e.target] // error("unknown module target: \($e.target)")) as $t |
    ($s|split("/")[0:2]|join("/")) as $sr |
    ($t|split("/")[0:2]|join("/")) as $tr |
    .["\($sr) -> \($tr)"] += 1
  ) | to_entries | sort_by(.value) | reverse | .[:100][] |
  "\(.value)\t\(.key)"
' "$GRAPH"
```
