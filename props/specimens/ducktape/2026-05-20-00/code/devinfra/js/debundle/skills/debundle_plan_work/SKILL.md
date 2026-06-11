---
name: debundle_plan_work
description: Plan and inspect generic JS debundle spec work using `debundle peel`. Use when an agent needs to turn owner_graph.json plus a modules tree into dispatchable module extraction work, query binding-patch status, inspect graph/source context, or decide what debundle spec edits should be made. Generic to any debundle target.
---

# Debundle Plan Work

Use this skill to plan read-only debundling work from the current
`owner_graph.json` and spec `modules/` tree. The output is evidence for
spec edits; this skill does not mutate YAML itself.

## Setup

Find the debundle output and spec modules directory for the target:

```bash
GRAPH=<debundle-output>/owner_graph.json
MODULES=<spec-root>/modules
SOURCE_ROOT=<upstream-js-root>
```

Build or run the debundle CLI. In a consuming Bazel repo, use the external
`@ducktape` label; inside the debundler repo, drop the repository prefix.

```bash
bazelisk run @ducktape//devinfra/js/debundle:debundle -- \
  peel plan-work --graph "$GRAPH" --modules "$MODULES" --limit 25 \
  >/tmp/debundle-plan.json
```

If `bazelisk run @ducktape//...` has Bazel server or output-download
trouble in a consuming repo, build the CLI with an isolated output base
and run the built binary directly:

```bash
bazelisk --output_base=/tmp/debundle-cli-bazel \
  build @ducktape//devinfra/js/debundle:debundle \
  --remote_download_outputs=all
```

## Planning Loop

1. Run `plan-work --limit 25` first. Treat `proposals[]` with
   `landable_today: true` as the primary dispatch surface. Each proposal
   has owner IDs, binding IDs, line span, active-module references, and
   residual-cell references. Limited output preserves planner order:
   residual-edge topo-depth, then source start line.

2. For binding-patch cleanup, run `patch-status`. Use `full[]` for patch
   sets that are currently assignable as a unit, `with_companions[]` when
   companion bindings must move together, and `near[]` for blocked patch
   sets worth investigating. An empty `patch-status` report only means no
   current binding-patch set matches those sections; it does not mean
   there are no peelable candidates.

3. For symbol-level candidates, run `candidates`. Use
   `--readable-only` when you want already-named bindings, and
   `--by-destination` when grouping by the heuristic destination helps.
   This is the best command for inspecting readable names that came from
   sidecar binding patches.

4. Before assigning a proposal, run `explain` on its proposal, owner, or
   binding ID. Check graph neighbors, current spec homes, peelability
   rows, and factorizer diagnostics.

5. Read source with `source-slice` when deciding final module names,
   architecture, or whether a proposal should be split further by hand.

## Commands

```bash
# Certified module-assignment proposals and diagnostics.
bazelisk run @ducktape//devinfra/js/debundle:debundle -- \
  peel plan-work --graph "$GRAPH" --modules "$MODULES" \
  --size-cap-lines 10000 --limit 25

# Binding-patch coverage and near misses.
bazelisk run @ducktape//devinfra/js/debundle:debundle -- \
  peel patch-status --graph "$GRAPH" --modules "$MODULES" \
  --near-missing 2 --max-companions 16 --limit 50

# Candidate catalog.
bazelisk run @ducktape//devinfra/js/debundle:debundle -- \
  peel candidates --graph "$GRAPH" --modules "$MODULES" \
  --readable-only --by-destination --limit 100

# Graph/spec explanation for one object.
bazelisk run @ducktape//devinfra/js/debundle:debundle -- \
  peel explain --graph "$GRAPH" --modules "$MODULES" \
  --proposal-id auto_partition_0000 --limit 25

# The selector can instead be --owner-id <owner> or --binding-id <binding>.
# There is no --binding shorthand.

# Source text for one object. Use --source-root when source_path is relative.
bazelisk run @ducktape//devinfra/js/debundle:debundle -- \
  peel source-slice --graph "$GRAPH" --modules "$MODULES" \
  --proposal-id auto_partition_0000 --source-root "$SOURCE_ROOT" \
  --context-lines 40
```

## Reading Results

- `plan-work` is the module-assignment proposal query.
- `patch-status` is the binding-patch coverage query, not the global
  candidate queue.
- `candidates` is the symbol-level candidate catalog.
- `explain` is the graph walk primitive for owners, bindings, and
  proposals. Select exactly one object with `--owner-id`, `--binding-id`,
  or `--proposal-id`.
- `source-slice` is the source retrieval primitive for the same IDs.

Prefer these commands over grepping generated output. The owner graph is
the source of truth for cycle gates, residual dependencies, and whether a
candidate is actually assignable.
