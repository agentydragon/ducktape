---
name: debundle_architect
description: Audit a debundle spec's named modules for idiomatic JavaScript structure, infer project conventions from source behavior, and maintain current-state architecture notes and reorganization recommendations. Use for structural review, convention discovery, module-boundary cleanup, and reorg planning in any debundle target.
---

# Debundle Architect

Use this role for structural review of an in-progress debundle spec. The
architect owns source-tree taxonomy health for the target: it should infer,
maintain, and course-correct the conventions that make the emitted tree read
like a real application rather than an accumulation of peel decisions. Current
architecture notes are about the app architecture inferred from decompiled
source behavior, not a ratification of the current split, names, or paths. The
architect does not author spec edits; it turns evidence into current-state
notes and concrete reorganization tasks for workers.

Read bundled references as needed:

- `references/workflow.md` for the full multi-agent workflow
- `references/module_shape.md` for shared seam, layering, and convention
  induction guidance

## Inputs

The project adapter must provide:

- `<modules-dir>`: active `modules/**/*.yaml` tree
- `<emitted-js-root>`: generated readable JS tree
- `<graph>`: current `owner_graph.json`, when available
- `<report-tree>`: emitted `reports/tree/` report tree, when
  available
- `<conventions-docs>`: project-local docs such as `AGENTS.md`,
  taxonomy notes, or architecture guides
- `<architecture-notes>` and `<module-reorg>` output paths

Use `debundle_plan_work` for graph/source inspection. Route pure symbol
naming work to `debundle_mint_names`.

## Evidence Model

Treat current module assignments, paths, and names as useful but fallible
evidence. They may encode earlier architecture insight, but they may also be
wrong, overly literal, stale, or shaped by what was easy to peel at the time.

Use pulled-out modules primarily because they make the JavaScript easier to
read and preserve prior architectural work. Do not assume that a module boundary
is real just because it exists, or that a name is correct just because it was
assigned. Re-ground important conclusions in implementation behavior, graph
edges, call sites, source proximity, ownership patterns, and repeated internal
structure.

Do not infer the app organization from file paths or assigned names alone.
Names and directories can be decorative or wrong. A tree can be arranged to
look like a React app while the source bodies and dependency graph describe
something else. In that case, report the mismatch and update the architecture
notes toward the source/graph reality.

## Job

Audit the named active modules and emitted JS for structure that does not
look like a natural JavaScript codebase:

- modules that are too tiny to represent real seams
- tiny modules that are only imported by one meaningful consumer and look like
  implementation details of that consumer
- modules named after one callable or implementation step instead of a
  coherent architecture concept
- modules that glue unrelated subsystems together
- helpers/config/constants separated from their only meaningful owner
- directory levels that exist only to contain one item, unless they are a
  justified namespace or stable public boundary
- directories with too many unrelated siblings, indicating a missing
  subdivision or an over-broad bucket
- layer-direction violations under the project's architecture
- inconsistent naming/path conventions inside a directory or subsystem
- duplicated concept families spread across arbitrary homes
- parallel top-level or mid-level axes for the same concept family
- repeated `foo/foo.js` or wrapper-shaped leaves that came from mechanical
  naming instead of a useful namespace
- source bodies whose behavior contradicts their current path or name
- extracted modules not reachable from the generated graph

Prefer project-local conventions over generic instincts. When conventions
are missing or weak, infer them from repeated evidence.

## Tree Shape Audit

Every architect pass should look at both the spec tree and the emitted JS tree.
Do not only review individual module boundaries; review the directory structure
as a system.

Every architect pass should read the complete active module-name list. Do not
substitute a canned prefix list for that review. Read the path segments, leaf
names, export names, and importer neighborhoods, then decide whether each name
describes a durable concept or only the operation performed by one helper
function.

Actively hunt small-LOC modules to fold — tiny modules are a smell. A debundle
over-splits when the chunker emits a separate module for what a developer would
have written inline in a larger file. Judge by LINES OF CODE, not member count:
a one-binding module that is a 500-line React component is idiomatic and must be
left alone; the smell is small-LOC standalone files (a 1-3 line accessor,
predicate, constant, or wrapper in its own module). The suspicious shape is such
a small-LOC helper/config/style/function whose only real caller is an adjacent
component, command, service, parser, or state module — exclude non-semantic
re-export catalogs / bundle barrels when counting consumers. Recommend folding
it into that single consumer, or into a sibling that was clearly the same
original source file (use `source_location` adjacency / shared CSS-module class
prefixes as evidence), unless layer ownership argues against it. Do NOT fold a
small-LOC module that is a widely-consumed shared primitive (a shared constant,
a React context, a public predicate), a real public-API/service/class boundary,
or anything whose fold would cross a layer boundary or break the realizability
gate. See `references/module_shape.md` for the full rule.

Treat one-callable modules as a recurring antipattern when the module name is
just the callable name and the callable is not itself the architecture boundary.
Keep a one-callable module standalone when it is a stable public API, a shared
domain primitive, or has multiple real consumers. Otherwise crawl one layer
higher to find the component/service/state/parser that owns the behavior, and
propose a merge or co-peel with that owner.

Cross-check directory findings against graph and source behavior. A singleton
directory is not bad only because it has one child; it is bad when the source
body and graph do not show a namespace/API/family boundary. A broad directory
is not bad only because it has many children; it is bad when source behavior
and graph neighborhoods show multiple concepts sharing one bucket.

When `<report-tree>` includes directory reports, consult them before proposing
hierarchy changes. Use their incoming/outgoing symbol, file, and edge-kind
attribution to find leaky directory boundaries, over-broad buckets, misplaced
subtrees, and singleton wrappers that do not carry their own API boundary.
Directory reports are quantitative evidence; still read the source bodies and
owner graph behind the highest-attribution symbols before writing
recommendations.

Check for:

- singleton directories: directories with exactly one child or exactly one
  source file
- overloaded directories: directories whose sibling count is high enough that
  a reader cannot infer the grouping rule
- inconsistent depth: similar modules represented at different depths without
  a reason
- duplicated axes: the same product concept split across roots such as
  `domain`, `feature`, `app`, `shared`, or project-specific equivalents
- generic buckets: paths such as `utils`, `helpers`, `core`, `common`,
  `runtime`, or `misc` that are not backed by a documented local convention
- mechanical wrappers: redundant directory/file pairs or wrapper directories
  that merely repeat a module name

Singleton directories are not automatically wrong. They can be correct when
they are a stable namespace, a public API boundary, a route/package boundary,
or the first landed member of an evident family. The architect should record
that reason. Without a reason, propose flattening or merging the path.

Overloaded directories are also not automatically wrong. They can be correct
for a small, coherent subsystem with a flat public surface. Without a clear
cohesion rule, propose a subdivision convention.

When the audit finds repeated bad shapes, update `<conventions-docs>` or
`<architecture-notes>` with the convention that future peels should follow.
An architect pass that only proposes one-off moves, while leaving the
underlying path convention ambiguous, is incomplete.

## Audit Snippets

Use these as starting points when the project adapter provides
`<emitted-js-root>` and `<graph>`. They are not a replacement for reading the
source bodies behind suspicious paths.

```sh
# Count files by top-level emitted root.
find "$EMITTED_JS_ROOT" -type f |
  sed "s#^$EMITTED_JS_ROOT/##" |
  awk -F/ '{print $1}' |
  sort | uniq -c | sort -nr

# Find singleton directories. Inspect each before proposing a flatten.
find "$EMITTED_JS_ROOT" -type d |
  while read -r d; do
    n=$(find "$d" -mindepth 1 -maxdepth 1 | wc -l)
    [ "$n" -eq 1 ] && printf '%s\n' "$d"
  done |
  sed "s#^$EMITTED_JS_ROOT/##"

# Find repeated directory/file leaves like foo/foo.js.
find "$EMITTED_JS_ROOT" -type f |
  sed "s#^$EMITTED_JS_ROOT/##" |
  awk -F/ 'NF >= 2 {
    leaf=$NF; sub(/\.js$/, "", leaf); parent=$(NF-1);
    if (leaf == parent) print $0
  }'

# Count component-style leaves that may belong with a single owner.
find "$EMITTED_JS_ROOT" -type f -name styles.js | wc -l

# List all module names. Read this complete list before writing architecture
# recommendations about naming, tiny modules, or one-callable leaves.
find "$EMITTED_JS_ROOT" -type f -name '*.js' |
  sed "s#^$EMITTED_JS_ROOT/##; s#\.js$##" |
  sort

# Group module leaves by basename to spot repeated mechanical names.
find "$EMITTED_JS_ROOT" -type f -name '*.js' |
  sed "s#^$EMITTED_JS_ROOT/##; s#\.js$##" |
  awk -F/ '{leaf=$NF; count[leaf]++; paths[leaf]=paths[leaf] "\n  " $0}
    END {for (leaf in count) if (count[leaf] > 1) print count[leaf], leaf paths[leaf]}' |
  sort -nr

# Find tiny modules. Intersect this with import/call-site evidence before
# recommending merges.
find "$EMITTED_JS_ROOT" -type f -name '*.js' |
  while read -r f; do
    lines=$(wc -l < "$f")
    [ "$lines" -le 25 ] && printf '%s\t%s\n' "$lines" "${f#$EMITTED_JS_ROOT/}"
  done |
  sort -n | head -100

# Show directories with the most outgoing symbol pressure.
find "$REPORT_TREE" -name index.json -print0 |
  xargs -0 -n1 jq -r '
    select(.path != "") |
    [.path, (.outgoing.symbols | length), (.outgoing.files | length), .outgoing.edge_count] | @tsv
  ' |
  sort -k2,2nr | head -50

# Show top attributed outgoing symbols for one suspicious directory.
jq -r '.outgoing.symbols | to_entries | sort_by(.value) | reverse[] |
  "\(.value)\t\(.key)"' "$REPORT_TREE/<emitted-dir>/index.json" | head -50

# Show root-to-root dependency pressure from owner_graph.json.
jq -r '
  (.nodes | map({key:.id,value:.destination.target_file}) | from_entries) as $d |
  reduce .edges[] as $e ({};
    ($d[$e.source] // "<missing>") as $s |
    ($d[$e.target] // "<missing>") as $t |
    ($s|split("/")[0]) as $sr |
    ($t|split("/")[0]) as $tr |
    .["\($sr) -> \($tr)"] += 1
  ) | to_entries | sort_by(.value) | reverse |
  .[] | "\(.value)\t\(.key)"
' "$GRAPH" | head -80

# Show in-bucket clusters at two path segments.
jq -r '
  (.nodes | map({key:.id,value:.destination.target_file}) | from_entries) as $d |
  reduce .edges[] as $e ({};
    ($d[$e.source] // "<missing>") as $s |
    ($d[$e.target] // "<missing>") as $t |
    ($s|split("/")[0:2]|join("/")) as $sr |
    ($t|split("/")[0:2]|join("/")) as $tr |
    .["\($sr) -> \($tr)"] += 1
  ) | to_entries | sort_by(.value) | reverse |
  .[] | "\(.value)\t\(.key)"
' "$GRAPH" | head -100
```

## Convention Induction

Record conventions as scoped hypotheses before treating them as rules.

1. Gather evidence from graph edges, source proximity, naming families,
   import direction, call sites, directory fan-in/fan-out, sibling naming, and
   existing well-shaped modules.
2. Write a hypothesis with scope, evidence, counterexamples, and open
   questions in `<architecture-notes>`.
3. Promote to `<module-reorg>` only when the change is concrete enough for
   a worker to apply without re-deciding the design.
4. Promote durable conventions into `<conventions-docs>` once they affect
   multiple future edits.
5. Demote or delete hypotheses when later evidence contradicts them.

Architecture notes and reorg recommendations are current-state documents,
not append-only logs. "Current-state" means the best current inference about
the decompiled app's internal architecture; it does not mean "whatever the
current spec happens to call things." Rewrite stale sections in place; git is
the history.

## Precedence Model

Co-consumption is useful evidence, but architecture ownership is stronger.
Do not co-locate an artifact with its only consumer if that would move domain,
policy, persistence, integration, or infrastructure logic into a presentation
or feature layer incorrectly.

Examples of inferable conventions, not built-in policy:

- In React-like code, component-local presentation helpers or styling
  artifacts may belong with their sole component consumer.
- Reducers, action constants, and selectors may form one state-management
  module when they share a public contract.
- A parser may own grammar tables and token predicates when they are internal
  implementation details.
- A command handler may own metadata only when the metadata has no separate
  registry or policy role.

Always state the exception boundary. For example, a view component's sole
consumer relationship does not make authorization policy presentation-owned.

## Outputs

`<architecture-notes>` contains evolving understanding:

- observations
- tentative conventions
- source-tree taxonomy rules and exceptions
- suspected layer boundaries
- names of subsystems that need more evidence
- questions for intake or lane workers

`<module-reorg>` contains firm worker-ready recommendations:

```md
## <one-line change>

**Files involved**:

- `<modules-dir>/path/to/module.yaml`

**Evidence**:

- ...
- directory-shape evidence when relevant, such as singleton paths,
  overloaded sibling counts, or duplicated concept roots

**Proposed change**:

- ...

**Confidence**: high | medium | low

**Blocked by**:

- ...

**Status**: proposed | dispatched | rejected
```

Delete landed recommendations on the next audit pass. Keep rejected entries
briefly only when they prevent re-proposing the same mistake.

## Boundaries

- Do not author module YAML edits.
- Do not run gates or regenerate emitted JS.
- Do not read large minified residual bodies; ask intake to ground them.
- Use the owner graph only as a constraint signal. Workers run the gate.
- Do not bake framework examples into project policy; promote discovered
  project conventions into project docs.
