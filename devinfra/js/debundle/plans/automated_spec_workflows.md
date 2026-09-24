# Automated Debundle Spec Workflows

Open product flows for spec automation: workflow contract, CLI shape and
milestones. Dispatch order is <../TODO.md>.

## Goal

Make debundle specs cheap to create, stabilize, and port across minified bundle
versions. The target user is an agent operating on a large real-world bundle:
the agent should spend most of its time reviewing ranked patch plans and
blockers, not hand-authoring fragile selectors one binding at a time.

Selector automation has two non-negotiable success criteria:

1. The produced spec must work for the current chunk and resolve the intended
   binding, group, or anonymous statement with a uniqueness proof.
2. The produced selectors must be likely to keep working across future versions
   of the minified chunks. A selector that copies an entire current function
   body, object literal, class body, or nested expression is often just an exact
   snapshot of today's code; it can be over-narrow even when it is unique. The
   tooling should prefer the loosest readable selector that still proves
   uniqueness, using holes and stable anchors instead of pinning incidental
   implementation detail.

This plan covers three product flows:

1. Given one or more minified JavaScript chunks, produce a minimal spec that
   emits readable, reviewable logical modules.
2. Given an existing partially unstable spec, replace fragile selectors with
   stable structural selectors in large batches.
3. Given version 1 chunks plus spec and version 2 chunks, update the spec to
   version 2 and present the remaining repair work as structured diagnostics.

Keep all examples and fixtures generic. Do not copy private downstream bundle
source into Ducktape tests.

## Desired Agent Workflow

An efficient agent loop should look like this:

1. Build an indexed source inventory for each chunk once.
2. Ask debundle for ranked worklists: unstable selectors, unresolved selectors,
   ambiguous selectors, repeated selector bodies, and likely grouping
   opportunities.
3. Ask debundle to synthesize a patch plan for a whole bucket, preferring
   minimized, forward-compatible selectors, with dry-run JSON/NDJSON explaining
   every applied and skipped candidate.
4. Apply the safe subset, preserving YAML comments and producing small
   deterministic diffs.
5. Run the keep-going validation report, not just the first failing selector.
6. Repeat until the residual report is small enough for manual review.

The important interface is the patch plan, not a single CLI command name. Every
bulk command should support path/module filters, `--apply`, stable JSON output,
and a reason for every skipped candidate.

## CLI Design Principles

Design commands around orthogonal operations, not one command per historical
rewrite:

- **inventory/report** commands read source/spec state and emit facts;
- **plan** commands turn facts plus intent into proposed edits;
- **apply** commands apply a previously emitted plan deterministically;
- **validate** commands check current state and emit keep-going diagnostics;
- **explain/show** commands inspect one target or failure in human-readable
  detail.

The same filters and output contracts should work everywhere they make sense:
`--module`, `--module-prefix`, `--file`, `--chunk`, `--source-root`,
`--format text|json|ndjson`, `--limit`, `--apply`, and `--plan-out` should not
mean subtly different things between commands. Prefer a small set of
composable verbs over a growing pile of bespoke flags whose behavior cannot be
piped into the next step.

Patch plans should be reusable artifacts: a dry-run plan can be reviewed,
filtered, applied later, or fed into a repair command. If a command cannot emit
a stable plan for an edit, it should remain a diagnostic/report command until
the edit model is clear.

## Core Data Model

### Source Inventory

The shared per-chunk index is `shape_index.rs` (over
`selector_candidate_index.rs`). Repair, port and bulk-planning tooling query it
rather than adding per-command AST walks that drift semantically.

### Selector Candidates

Candidate generation should produce multiple selector forms for the same target
and let a ranker choose:

- exact structural `source_match`;
- minimized structural `source_match` with `ANYTHING`, `DECLARATORS`, `ARGS`,
  and `STMT_LIST` holes;
- grouped `source_matches[]` entries when one source context exports multiple
  bindings;
- literal-initializer selectors when a top-level binding is uniquely
  initialized to a stable literal;
- regex string-literal anchors for generated class/style names or other stable
  prefix/suffix families;
- contextual windows when a selected statement is generic but nearby stable
  anchors make the window unique;
- anonymous-statement and statement-list selectors for side-effect blocks.

Selectors should be ranked by a cost model that rewards uniqueness, stability,
small readable source, grouped exports, exact stable literals/keys, and fewer
unneeded pinned generated details. Prefer wildcards over long code or
subexpressions when both forms uniquely select the same target. Exact source
should be retained only for stable signal: declaration kind, target binding
position, stable literal/key/operator/callee shape, ordering, or a small
context window that distinguishes otherwise ambiguous candidates.

### Minimizer

"Produce the loosest readable selector that uniquely selects this entity" is a
first-class operation; open minimizer polish lives in <../TODO.md>.

"Simplest" means
lowest-cost-forward-compatible, not shortest or exact-source — prefer low-cost
stable anchors that cut the candidate set sharply, assign high cost to long exact
function/object/class bodies, statement runs, and nested expressions, and
**report over-narrow selectors as debt even when they currently match** (long
function bodies where a signature + stable literal would suffice; object literals
where a few stable keys suffice; class bodies where `ANYTHING;` keeps the useful
member; anonymous blocks where `STMT_LIST` ignores setup/cleanup). Grouped
`source_matches[]` entries use the same cost model, comparing one shared selector
against repeated selectors and splitting when one huge selector would need long
exact bodies or volatile initializers to be unique.

**Reframe.** The cost model ranks and _proves_ candidates but does not _choose_
the anchor: picking a purpose-bearing,
forward-compatible anchor over a merely-unique one (the `name`-key vs `"running"`
problem) is an intelligence task delegated to an agent, with the minimizer demoted
to a suggester and the prove-gate kept as the validity oracle. The over-pin backlog
tracked in <../TODO.md> is then about better _defaults_, not about spec quality.

### Patch Plans

Bulk tools should produce a machine-readable plan before mutating YAML:

- target module path and member/group/anonymous-statement location;
- current selector summary and proposed selector summary;
- edit kind: replace selector, create binding group, merge members, add hole,
  add target binding, add diagnostic comment, or leave unchanged;
- proof: uniqueness result, body indices, target binding, grouped exports, and
  source-inventory anchors used;
- skip reason with the next useful action.

Apply mode should preserve comments and ordering where possible. If a rewrite
needs to delete repeated member-form selectors and create one group, comments
from the deleted members must be retained or moved into the group/export rows.

## High-Level CLI Shape

Exact names can change, but the command surface should stay consistent and
orthogonal:

- `debundle spec inventory --modules ... --source-root ... --format ndjson`
  emits source/spec facts: bindings, anonymous statements, current selectors,
  debt buckets, grouping opportunities, source fingerprints, and stable anchors.
- `debundle spec plan selectors --targets <targets.json> --source-root ...`
  emits a patch plan that creates or replaces selectors for explicit bindings,
  groups, anonymous statements, or statement ranges.
- `debundle spec plan stabilize --modules ... --source-root ...`
  emits a patch plan for eligible fragile selectors across an existing spec.
- `debundle spec plan repair --diagnostics <report.json> --source-root ...`
  emits a patch plan for the unresolved, ambiguous, duplicate-claim, or invalid
  outcomes of `spec validate --format json`.
- `debundle spec plan port --from-modules ... --from-source-root ... --to-source-root ...`
  emits a patch plan carrying a spec across bundle versions plus residual
  semantic tasks.
- `debundle spec apply-plan --plan <plan.json> --modules ...`
  applies a reviewed plan, preserving comments/order where the edit type
  supports it and refusing stale plans whose inputs no longer match.
- Existing focused commands such as `selector-debt` and `selector-codemod` can
  remain as aliases or transitional frontends, but new work should converge on
  the inventory/plan/apply/validate model.

Prefer extending existing commands when that keeps the UX coherent. Add new
verbs when overloading an existing command would make the mode hard to explain.

## Flow 1: New Chunks to Readable Spec

The bootstrap flow should combine existing debundle planning with selector
synthesis:

1. Run module/factor proposals and naming passes to identify candidate logical
   modules and export names.
2. For each proposed binding cluster, synthesize a selector or binding group.
3. Prefer grouped declaration contexts over repeated member-form selectors.
4. Use the minimizer to remove irrelevant initializer arguments, object
   properties, class members, and statement ranges while preserving uniqueness.
5. Emit an initial spec plus a report of low-confidence names, unaddressable
   anonymous statements, and selectors that needed high-cost context.

This should make new-app specs start structurally stable instead of beginning
with a large name-only debt pile.

## Flow 2: Stabilize an Existing Spec

The stabilization flow is the current large-spec migration case:

1. Inventory debt with `selector-debt` and bucket by mechanical rewrite class.
2. Apply broad buckets first: class declarations, function declarations,
   literal-initialized constants, multi-declarator groups, style/string regex
   anchors, object-literal anchors, and anonymous statement lists.
3. For each bucket, generate a patch plan and apply only candidates with a
   uniqueness proof.
4. Record precise YAML comments for candidates that cannot yet be stabilized,
   including the missing selector feature or ambiguity reason.
5. Track debt metrics after each batch: name-only count, repeated selectors,
   unresolved selectors, ambiguous selectors, and slow selector families.

The output should be large reviewable PRs grouped by rewrite class, not tiny
hand-authored module-by-module edits.

## Flow 3: Port Version 1 Spec to Version 2 Chunks

Porting is step 3d of <selector_engine.md>. The retrieval design in "Repair
and porting search" below applies to it.

## Repair Workflow

For a selector that does not match anything:

1. Classify the failure: parse/schema error, unsupported hole, free readable
   identifier, no top-level candidate, local subtree mismatch, literal mismatch,
   or context-window mismatch.
2. Show nearest candidate statements using canonical AST distance and stable
   anchor overlap.
3. Try mechanical relaxations: replace volatile subtrees with holes, shrink or
   expand the statement window, use literal/regex anchors, or group bindings.
4. Emit a candidate patch only if the repaired selector is unique.

For an ambiguous selector:

1. List candidate source identities and the anchors they share.
2. Suggest the smallest differentiating stable anchor: literal, key, class
   member, call shape, adjacent statement, or declaration kind.
3. If no stable differentiator exists, report that the selector must remain
   intentionally more specific or use a different ownership boundary.

For duplicate claims:

1. Report duplicate identity by declaration/declarator, not only minified name.
2. Identify whether the right rewrite is binding-group collapse,
   cross-module-group support, or a real ownership conflict.

## Performance Plan

The resolve already parses each chunk once, prepares each selector once and
prunes candidates through an index (<../docs/selector_resolution.md>). Open:
memoize selector resolution across invocations, keyed by chunk hash, normalized
selector, target and Ducktape version. Update `perf/` notes from real profiles
before major matcher rewrites.

### Scale Model

Use these symbols when designing and reviewing algorithms:

- `N`: top-level statements in a chunk;
- `B`: top-level declared bindings/declarators in a chunk;
- `S`: selectors or selector targets being processed;
- `L`: average selector AST size;
- `K`: candidate statements after indexed filtering.

The downstream-scale target is thousands of YAML modules and thousands of
selectors. The interactive budget (under 10s warmed, over 60s blocking unless
explicitly offline) is <../docs/selector_resolution.md> § Interactive budget.

Expected warmed runtime budgets on downstream-scale inputs:

| Operation                                                   |       Target |  Hard concern |
| ----------------------------------------------------------- | -----------: | ------------: |
| Build source inventory for one large chunk                  |         1-3s |          >10s |
| Emit selector debt/inventory report for the current spec    |         <10s |          >60s |
| Classify name-only function/class/declarator rewrite bucket |          <5s |          >30s |
| Plan a broad mechanical stabilization bucket                |         <10s |          >60s |
| Minimize one ordinary selector context                      |     10-100ms |           >1s |
| Validate selectors in keep-going mode after a batch         |         <10s |          >60s |
| Deep nearest-candidate repair search                        | offline mode | no silent run |

If a command cannot meet the interactive budget, it should stream progress,
write resumable intermediate plans, and label itself as offline/profile mode.

### Bulk Stabilization

Stabilizing an existing spec should operate by buckets:

- exact name-only function/class/declarator candidates via binding-name index:
  `O(S + B)` to classify, then one uniqueness proof per candidate;
- literal-initialized constants via literal initializer index:
  `O(B)` index build plus `O(1)` lookup per candidate value;
- repeated selector bodies via normalized body hash:
  `O(S * L)` normalization and `O(S log S)` or hash grouping;
- repeated declarator-context selectors into binding groups:
  group by resolved statement id and declarator set, then emit one edit per
  group;
- overpinned objects/classes/functions:
  synthesize once per unique source context, then fan out to all members using
  that context.

The critical rule is to dedupe before expensive matching. A spec with 8.5k
fragile selectors should not do 8.5k independent parse/prepare scans if many
selectors share context or rewrite class.

### Repair And Porting Search

No-match and version-port repair should use top-k retrieval rather than scanning
every statement:

1. Extract anchors and fingerprints from the failing v1 selector or selected
   v1 source identity.
2. Retrieve candidate statement ids from high-value anchors first: stable
   string literals, object keys, property names, declaration kind, arity, and
   structural fingerprint prefix.
3. Score candidates by weighted anchor overlap and AST edit distance on the
   small retrieved set.
4. Run selector synthesis/minimization only for the top candidates.

Expected cost per failed selector should be near `O(sum selective postings +
K log K + K * diff_cost)`, with `K` capped. A full `O(N * diff_cost)` nearest
candidate scan is acceptable only as an explicit deep diagnostic mode.

### Patch Application And Caching

Patch planning and application should be deterministic:

- include input hashes for source chunks, modules files, selector bodies, and
  Ducktape version in patch-plan metadata;
- refuse apply when the input hash no longer matches, unless the caller asks to
  rebase/recompute the plan;
- cache inventory and prepared selector data by chunk hash and Ducktape
  version within a process, and leave room for a future on-disk cache;
- stream NDJSON plan rows as soon as they are known so long runs remain useful
  and interruptible.

## Implementation Milestones

### Milestone 3: Repair Reports and Patch Plans

- Nearest-candidate and smallest-differentiator diagnostics for `no_match` and
  `ambiguous` outcomes.
- `plan repair` consumes the `spec validate --format json` report and emits or
  applies patch plans for mechanically proven cases.

### Milestone 4: Version-Port Workflow

Step 3d of <selector_engine.md>.

### Milestone 5: New-App Bootstrap

- Feed module proposals, naming output, and selector synthesis into an initial
  spec generator.
- Produce a reviewable spec plus confidence/debt reports for the first human
  pass.

## Success Metrics

- A large existing spec can remove hundreds or thousands of fragile selectors
  per PR, grouped by rewrite class.
- Generated selectors are unique by construction and validated by the existing
  resolver.
- Manual YAML editing becomes the fallback for semantic decisions, not the
  normal path for mechanical selector conversion.
- Keep-going diagnostics surface the full repair backlog in one run.
- Selector matching time is dominated by the few genuinely complex selectors,
  not repeated reparsing or avoidable quadratic scans.
- New downstream debundle specs start with structural selectors and an explicit
  debt report.
