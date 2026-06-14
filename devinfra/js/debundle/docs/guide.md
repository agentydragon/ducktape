# Debundle User Guide

Step-by-step workflows for the `debundle` CLI. The command surface
itself is in `docs/cli.md`; this document is the operational companion.

## Setting common env vars once

Every read-only and mutating command accepts `--graph`, `--modules`,
and (for source-reading commands) `--source-root`. Export the
corresponding env vars once per shell session and subsequent commands
run without repeating the flags:

```bash
export DEBUNDLE_GRAPH=<debundle-output>/reports/tree/<chunk-id>/owner_graph.json
export DEBUNDLE_MODULES=<spec-root>/<version>/modules
export DEBUNDLE_SOURCE_ROOT=<debundle-output>/app
export DEBUNDLE_OUT=<debundle-output-root>
```

Flags win when both are set. Use this for one-off overrides.

`DEBUNDLE_SOURCE_ROOT` feeds only the query commands' `--source-root`
(the upstream snapshot root). `debundle run --tree-source-root` — the
spec-tree compile root — reads the separate `DEBUNDLE_TREE_SOURCE_ROOT`,
since the two roots are different directories in real corpora.

If remote execution downloads only minimal outputs, request full outputs
so side files are local:

```bash
--remote_download_outputs=all
```

## Output formats

Read-only commands accept `--format text|json|ndjson`:

- `text` — interactive default, scannable on a terminal.
- `json` — single JSON document, parseable with `jq`.
- `ndjson` — one JSON value per line, for streaming consumers.

If `--format` isn't passed and stdout is **not** a tty, the default
flips to `json`. So `debundle modules propose | jq ...` works without
an explicit `--format json`.

Reach for `ndjson` on streaming queries with many rows
(`debundle scc --format ndjson` over a large graph), or when piping
into `jq -c` / `xargs`.

Mutating commands (`bindings assign`, `bindings rename`, `modules
merge`) print a one-line verdict (`ok`, `would change N files`,
`rejected ...`). `--dry-run` adds the planned diff summary; a
structured YAML diff is not yet in v1.

## Running the pipeline

Run the transform pipeline:

```bash
debundle run --spec <transform-spec.yaml>

debundle run \
  --tree-config <spec-config.yaml> \
  --tree-modules <modules-dir> \
  --tree-vendor-marks <vendor-marks.yaml> \
  --out-root <out-dir>
```

The gate is part of the pipeline contract: if the spec is
unrealizable, `debundle run` rejects and emits structured side outputs
(`cycles.json`, `atomic_unit_conflicts.json`) under
`reports/tree/<chunk-id>/`. There is no `run --no-verify` — fix the
spec first.

Use `debundle run --dry-run` to run pipeline parse/facts/gate checks
without writing emitted JS or reports.

Broad spec migrations continue through supported diagnostic failures by
default and report all findings from that pass. Today this aggregates
unresolved source-match selectors and duplicate binding claims during
materialization plan building, including the chunk, binding, existing
owner/export/origin, and competing owner/export/origin. Use `--fail-fast` only
when the first failing selector or claim is the useful debugging target.

## Workflow: investigating a binding end-to-end

When a binding's role is unclear or a proposal is suspicious:

1. **`debundle describe <sym>`** — graph + spec context. `<sym>` is
   either the minified name (`XOe`) or the readable name
   (`PluginSettingsAccessor`). Output includes the binding's owner,
   home module, atom membership, and incoming/outgoing edges. Add
   `--include-proposals` only when factorizer proposal/diagnostic
   annotations are needed.
2. **`debundle show-source <sym>`** — print the original source span
   for the owner. Use `--context-lines 40` to widen the view.
3. **`debundle cluster <sym>`** — list the module-quotient neighbors
   of the binding's owner. Useful for "what does this module touch?"
   questions before deciding a destination.

`describe` and `show-source` accept any ID kind: bindings, module
paths (`runtime/plugins`), owner IDs (`owner:42`), atom IDs, proposal
IDs, diagnostic IDs. The renderer dispatches on the kind it detects.

## Workflow: proposing new modules

Use the factorizer to surface what's currently extractable:

1. **`debundle modules propose --format json > proposals.json`** —
   factorizer proposals + diagnostics derived from the atomic DAG.
2. **Skim the diagnostics.** Each diagnostic carries a `reason`
   explaining why a closed atomic-DAG set could not become a proposal
   (currently `exceeds_size_cap`: the spec edit is larger than
   `--size-cap-lines`).
3. **Apply reviewed binding-only proposals** (see the move workflow
   below). `bindings assign --batch` accepts selected proposal objects
   when every selected row maps to member moves: `landable_today: true`,
   non-empty `binding_ids`, no `merge_into`, and no
   `anonymous_statement_owner_ids`.

```bash
jq '[.proposals[]
     | select(.landable_today
       and (.merge_into | not)
       and (((.binding_ids // []) | length) > 0)
       and (((.anonymous_statement_owner_ids // []) | length) == 0))]' \
  proposals.json > selected-proposals.json
debundle bindings assign --batch selected-proposals.json --dry-run
```

`merge_into` rows are emitted-output proposal evidence, not direct
`bindings assign` moves; use `debundle modules merge --target ...` or
manual YAML after choosing the target. Rows with
`anonymous_statement_owner_ids` need `anonymous_statements:` edits.

Rows with `status: blocked_residual_dependency` are never
`landable_today`: the cell reads other residual cells
(`other_residual_cells_referenced`), so promoting it alone would trip
the realizability gate. Grow the closure — assign the proposal
together with the referenced cells in one batch — or co-locate the
owners manually before assigning.

For aggregate counts before drilling in:

```bash
debundle graph-summary --format text
```

Use `debundle graph-summary --include-proposals` only when aggregate
proposal and diagnostic counts matter; the default is a fast graph
summary.

For a focused look at one proposal before applying:

```bash
debundle describe <proposal-id>
debundle show-source <proposal-id> --context-lines 40
```

## Workflow: moving a binding from one module to another

`debundle bindings assign` is the move primitive. Each positional
argument is colon-separated: `<sym>:<module>[:<readable>]`.

### Single move, keep current name

```bash
debundle bindings assign XOe:runtime/plugins
```

`<sym>` accepts minified (`XOe`) or readable
(`PluginSettingsAccessor`) form. The destination module is
auto-created if it doesn't yet exist; the source module is auto-deleted
when its `members:` becomes empty **and** its top-level `comment:` is
empty/absent (modules with a comment are kept as `members: []`
shells).

### Move + rename in one step

```bash
debundle bindings assign XOe:runtime/plugins:PluginSettingsAccessor
```

The optional third field sets the new readable `name:`. Validation
includes name-collision detection.

### Batch move

Multi-binding refactors run as one atomic operation: validation is
checked on the post-batch spec, not after each individual move. This
lets the intermediate state be "invalid" (e.g. half-moved atom) so
long as the final state is valid.

```bash
debundle bindings assign \
    XOe:runtime/plugins:PluginSettingsAccessor \
    YOe:runtime/plugins \
    ZOe:runtime/widgets:WidgetRegistry
```

For large refactors, pipe JSON:

```bash
debundle bindings assign --batch moves.json
debundle bindings assign --batch - < moves.json
```

JSON shape:

```json
[
  { "sym": "XOe", "module": "runtime/plugins", "readable": "PluginSettingsAccessor" },
  { "sym": "YOe", "module": "runtime/plugins" },
  { "sym": "ZOe", "module": "runtime/widgets", "readable": "WidgetRegistry" }
]
```

`--batch` also accepts `modules propose --format json` output, or a
filtered proposal array, when every selected proposal maps cleanly to
member moves. It refuses non-landable rows, `merge_into` rows, rows
without `binding_ids`, and rows containing
`anonymous_statement_owner_ids`; use an explicit move array when you
need `readable` renames.

`sym` and `module` are required; `readable` is optional. Array order
controls dedupe (last-wins on duplicate `sym`).

### Default validation, `--dry-run`, `--no-verify`

```bash
# Default: validate the post-batch spec; refuse if invalid; apply if valid.
debundle bindings assign XOe:runtime/plugins

# Preview only: run validation, print verdict, don't modify any file.
debundle bindings assign --dry-run XOe:runtime/plugins

# Apply without validating. Escape hatch for intentional intermediate states.
debundle bindings assign --no-verify XOe:runtime/plugins
```

`--dry-run` + `--no-verify` together: show what would change without
validating. Useful for inspecting an intermediate that you know
violates the gate.

## Workflow: authoring portable selectors

When the source bundle has autogenerated or minified identifiers, avoid
name-only selectors as a general authoring rule. Those names are not
semantic handles: they can drift between versions, differ between chunks,
or accidentally refer to a different symbol after a source refresh. Prefer
structural selectors that describe the stable AST shape and let debundle
resolve the current runtime binding.

Default to this ladder when writing or repairing selectors:

1. Use `selector.source_match` / `binding_groups` for the declaration,
   statement, class, or small surrounding AST shape that is semantically
   stable.
2. Add anonymous `ANYTHING` holes where the hole name would not carry useful
   signal. Use typed holes (`EXPR`, `STMT`, `ARGS`, `STMT_LIST`,
   `OBJECT_PROPS`, `CLASS_REST`, or `DECLARATORS`) when the syntactic role, a
   typed list hole, or a named/equality hole makes the selector easier to read
   or diagnose.
3. Keep exact literals, property names, object keys, operators, and ordering
   when they carry the stable signal.
4. Use `selector.binding.name` only for already-stable semantic names or as
   temporary debt that will be visible in `debundle spec selector-debt`.

When several `members[].selector.source_match` entries repeat the same
multi-declarator selector with different `target_binding` values, run
`debundle spec selector-debt --source-file <chunk.js> --modules <modules-dir>`.
The source-aware report emits `binding_groups` suggestions that collapse those
member selectors into one `source_match` with `DECLARATORS_*` holes and an
`exports:` map.

Use `selector.source_match` for stable declaration shapes:

```yaml
members:
  - name: selectedConfig
    selector:
      source_match:
        identifiers: alpha_all
        match: 'const config = { kind: "selected", enabled: true };'
```

When a dry run spends too long resolving selectors, rerun with
`DUCKTAPE_SOURCE_MATCH_TIMINGS=1` to print one stderr line per
`source_match` resolution. Set `DUCKTAPE_SOURCE_MATCH_TIMING_THRESHOLD_MS`
to show only selectors at or above that duration. Each line names the module,
selector kind, match count or resolved binding, elapsed milliseconds, and a
compact selector preview. It also includes `selector_key` and `body_key`
hashes: group by `body_key` to find duplicated expensive selector bodies, and
by `selector_key` to find identical selector+target forms. For private bundle
logs, set `DUCKTAPE_SOURCE_MATCH_TIMING_PREVIEW=0` to hide selector source while
keeping keys, body indices, request ids, and binding summaries.

`identifiers: alpha_all` treats binding/value identifiers in the selector
as alpha-renamable placeholders while keeping literals, operators,
member property names, object keys, and AST structure significant. This
is useful for matching `function(x, y) { return x * z; }` against the
same structure after minifier parameter names drift.

Use `target_binding` when the readable selector includes more than one
binding but the member exports only one of them:

```yaml
members:
  - name: selectedLocalPart
    selector:
      source_match:
        identifiers: alpha_all
        target_binding: localPart
        match: |
          const localPart = "primary",
            domain = "example.test",
            address = `${localPart}@${domain}`;
```

When the selected declaration is too generic by itself, include adjacent
top-level statements as context in the same `match`. The whole statement range
must match uniquely, but only `target_binding` is claimed:

```yaml
members:
  - name: selectedSessions
    selector:
      source_match:
        identifiers: alpha_all
        target_binding: sessions
        match: |
          const sessions = new Map(EXPR);
          function closeSessions() {
            return sessions.clear();
          }
          function getSession(name) {
            return sessions.get(name);
          }
```

If the selected binding is one declarator inside a wider comma-list declaration,
write only the selected declarator in the selector and keep `target_binding` on
that selector-local name. Debundle can align that single declarator to the
matching declarator inside the runtime `var`/`let`/`const` list while still
using any adjacent selector statements as context:

```yaml
members:
  - name: selectedConfig
    selector:
      source_match:
        identifiers: alpha_all
        target_binding: config
        match: |
          const config = { kind: "selected" };
          function readConfig() {
            return config.kind;
          }
```

Use `DECLARATORS_BEFORE` / `DECLARATORS_AFTER` or `binding_groups` when several
declarators in the same comma-list need to be exported together. Do not spell
unrelated sibling declarators just to make a single binding selectable.

Use `binding_groups` when several exports come from the same
multi-declarator or short declaration context. This is sugar for several
`members[].selector.source_match` entries with the same `match` and
different `target_binding` values:

```yaml
binding_groups:
  - source_match:
      identifiers: alpha_all
      match: |
        const localPart = "system",
          domain = "example.test",
          address = `${localPart}@${domain}`;
    exports:
      localPart: SYSTEM_EMAIL_LOCAL_PART
      domain: SYSTEM_EMAIL_DOMAIN
      address: systemEmailAddress
    comments:
      address: Primary address shown to operators.
```

The `match` may also be a contiguous range of top-level declarations. This is
useful when related constants are declared separately and a later object ties
them together:

```yaml
binding_groups:
  - source_match:
      identifiers: alpha_all
      match: |
        const firstClassName = STR_LITERAL_MATCHING_RE("^Widget-module_first__[A-Za-z0-9_-]+$");
        const secondClassName = STR_LITERAL_MATCHING_RE("^Widget-module_second__[A-Za-z0-9_-]+$");
        const styles = { first: firstClassName, second: secondClassName };
    adopt_names: true
```

Under `alpha_all`, the selector-local `firstClassName` and `secondClassName`
names carry across the range, so the object initializer can refer to the
matched runtime bindings without spelling their minified names.

Declaration ranges are still structural AST matches. Comments between
declarations are ignored, but export syntax is not: match `export const ...`
with `export const ...` in the selector. A range may mix declaration kinds,
such as `const` declarations followed by a helper `function` and another
`const`. The `exports` map and `adopt_names` only choose which matched
bindings are public exports; unexported declarations in the range may still be
carried into the selected module or imported from residual output as ordinary
dependencies of the exported bindings.
If a range misses, the diagnostic names `binding_groups[].source_match` and
reports that no top-level declaration range matched; common causes are
non-contiguous source statements, `export` syntax present on only one side, or
an exact literal/object/function body mismatch outside a deliberate hole.

When the selector source already uses the desired public names, use
`adopt_names` instead of repeating identity entries under `exports`:

```yaml
binding_groups:
  - source_match:
      identifiers: alpha_all
      match: |
        const SYSTEM_EMAIL_LOCAL_PART = "system",
          SYSTEM_EMAIL_DOMAIN = "example.test",
          systemEmailAddress = `${SYSTEM_EMAIL_LOCAL_PART}@${SYSTEM_EMAIL_DOMAIN}`;
    adopt_names: true
```

`adopt_names: [nameOne, nameTwo]` adopts only the listed selector-local
bindings. An explicit `exports` entry on the same group overrides the adopted
public name for that selector-local binding. `comments` is keyed by
selector-local binding name and emits like `members[].comment` after expansion.

When the same source window should export bindings and move adjacent
side-effect statements, put `target_statement` or `target_statements` on the
binding group's `source_match`. Binding exports are still controlled by
`exports` / `adopt_names`, while the selected statement indices are claimed as
anonymous statements from the same structural match:

```yaml
binding_groups:
  - source_match:
      identifiers: alpha_all
      target_statements: [1, 2]
      match: |
        let recordSlot, replaySlot;
        true && (replaySlot = "replay");
        false || (recordSlot = "record");
    exports:
      recordSlot: recordBridge
      replaySlot: replayBridge
```

Decorator/property setup calls fit the same pattern. Use the class declaration
as the binding context, keep durable property strings exact, and claim the
decorator calls by index:

```yaml
binding_groups:
  - source_match:
      identifiers: alpha_all
      target_statements: [1, 2]
      match: |
        class DecoratedModel {
          CLASS_REST;
          report() {
            STMT_LIST;
          }
          CLASS_REST;
        }
        decorate(["observable"], DecoratedModel.prototype, "state");
        decorate(["observable"], DecoratedModel.prototype, "title");
    adopt_names: [DecoratedModel]
```

Under `alpha_all`, `decorate` and `DecoratedModel` are selector-local names:
they only require a consistent structural correspondence inside this selector,
so the runtime helper and class binding may be minified differently. The
strings `"state"` and `"title"` remain exact anchors.

When a few useful bindings sit inside a larger `var`/`let`/`const` declaration
list, use declarator-list holes instead of spelling unrelated siblings:

```yaml
binding_groups:
  - source_match:
      identifiers: alpha_all
      match: |
        const DECLARATORS_BEFORE = null,
          selectedFormatter = EXPR_FORMATTER,
          selectedLabels = new Map([
            ["left", "Left"],
            ["right", "Right"],
          ]),
          selectedReader = EXPR_READER,
          DECLARATORS_AFTER = null;
    adopt_names: true
```

`DECLARATORS` and `DECLARATORS_*` absorb any run of declarators, including an
empty run. The initializer is ignored; selectors commonly write `= null`
because `const` declarations require an initializer. These pseudo-declarators
are not exposed through `adopt_names`. This is the preferred pattern when a
few adjacent constants or arrow-function helpers sit at the beginning, middle,
or end of a larger declaration list: pin the declarators you want to export,
put `DECLARATORS_BEFORE` and/or `DECLARATORS_AFTER` around the unrelated
siblings, and use `adopt_names` or explicit `exports` only for the pinned
selector-local names.

For anonymous side-effect statements, prefer `source_match` when
minified helper or class names drift, but keep selectors unique. If two
statements are structurally identical under `alpha_all`, debundle must
reject the spec as ambiguous rather than picking by source order.
Refine the selector with an exact statement, a stable literal/property
difference, or a deliberately small surrounding context.

When the anonymous statement needs surrounding declarations for a unique
structural match, include that context and set `target_statement` to the
zero-based index of the statement to claim:

```yaml
anonymous_statements:
  - source_match:
      identifiers: alpha_all
      target_statement: 1
      match: |
        const selectedLeft = "left",
          selectedRight = "right";
        console.log(`${selectedLeft}:${selectedRight}`);
```

Only the indexed statement is claimed; the surrounding context is used for
matching and must be claimed separately if it should move with the anonymous
statement.

Use `target_statements` when one structural selector should claim several
anonymous statements. It accepts either an explicit zero-based index list, or
`all` to claim every non-hole top-level statement in the selector:

```yaml
anonymous_statements:
  - source_match:
      identifiers: alpha_all
      target_statements: [1, 2]
      match: |
        const contextValue = "stable";
        console.log("first side effect");
        console.log("second side effect");

  - source_match:
      identifiers: alpha_all
      target_statements: all
      match: |
        console.log("before");
        STMT_LIST;
        console.log("after");
```

At top level in an anonymous-statement selector, `STMT_LIST;` absorbs a run of
module-body statements that should be used only as skipped context. A
`target_statement` or `target_statements` index must point at a pinned
statement, not at the `STMT_LIST` hole.

Do not solve ambiguity with opaque hashes. A selector should be readable
enough for a reviewer to audit and edit. When an anonymous statement needs
nearby declarations to be unique, prefer `target_statement` over spreading
duplicated boilerplate.

Use syntactic holes as the normal way to keep structural selectors portable:
pin the stable skeleton, and leave unstable minified details as holes instead
of falling back to a name-only selector. They work well for small volatile
subtrees that are not stable enough to spell exactly:

```yaml
members:
  - selector:
      source_match:
        identifiers: alpha_all
        match: |
          var x = foo(EXPR_LEFT, EXPR_RIGHT, x);
    name: resolvedValue

anonymous_statements:
  - source_match:
      identifiers: alpha_all
      match: |
        if (ready) {
          STMT_REGISTER_PRELUDE;
          register(Service);
        }
```

Single-node holes have two forms. The **bare keyword** is an anonymous
wildcard: every occurrence matches independently, so there's no need to mint a
unique name per throwaway placeholder. The **named form** `KEYWORD_name` binds
for cross-occurrence equality — the same name must match the same candidate
subtree/statement everywhere it appears. So `EXPR` is the identifier
expression that matches one arbitrary expression subtree, and `EXPR_left`
matches one too but forces every `EXPR_left` to be the same subtree; `STMT` and
`STMT_setup` are the single-statement equivalents. For example, `foo(EXPR)`
matches both `foo(123)` and `foo(456)`, and `bar(EXPR, EXPR)` matches
`bar(1, 2)` (the two holes are independent), whereas `bar(EXPR_x, EXPR_x)` only
matches a call whose two arguments are identical. These are still structural
selectors: surrounding syntax is exact after the identifier policy is applied,
and ambiguous matches are rejected rather than resolved by source order.
Reserved future selector spellings fail before matching with an explicit
`unsupported selector capability` diagnostic. For example, `ANYTHING_FUTURE`
is rejected early instead of falling through to a generic no-match report.
Unknown `source_match` fields use the same diagnostic class.

`ANYTHING` is universal anonymous sugar for positions where raw JavaScript can
parse a plain identifier and the matcher already has a typed hole for that AST
position:

- In expression position, `ANYTHING` behaves like anonymous `EXPR`.
- As a bare statement (`ANYTHING;`), it behaves like anonymous `STMT`.
- As a non-declarator binding pattern, it matches any candidate pattern. This
  is useful for parameters or destructuring when the stable signal is in the
  body, not the binding shape.
- As an object-literal shorthand property, it absorbs a run of key/value
  properties or spreads:
  `{ required: EXPR, ANYTHING, other: EXPR }`.
- As a variable declarator (`const ANYTHING = null, selected = ...;`), it
  behaves like anonymous `DECLARATORS` and absorbs a run of sibling
  declarators. The initializer is ignored, as with `DECLARATORS`.
- As a class field with no initializer (`class K { ANYTHING; method() {} }`),
  it behaves like `CLASS_REST`.

Prefer `ANYTHING` for throwaway holes whose name would only be noise. For
object literals and destructuring-heavy selectors, match only enough stable
anchors to identify the target uniquely, and no more: put `ANYTHING` where
arbitrary unrelated structure may appear instead of spelling it.

```js
const config = {
  requiredKey: ANYTHING,
  ANYTHING,
  anotherKey: ANYTHING,
};
```

Keep the typed spelling when the role is not obvious from nearby syntax, when
you need a named equality hole such as `EXPR_value`, or when you need typed
list-hole forms such as `ARGS`, `STMT_LIST`, or `OBJECT_PROPS_GENERATED`.
`{ key: ANYTHING }` wildcards the value expression; `{ ANYTHING: value }` is
rejected because object property keys are exact anchors, not wildcard
positions.

For string literals with stable shape but unstable suffixes, use
`STR_LITERAL_MATCHING_RE("...")` in expression position:

```yaml
members:
  - selector:
      source_match:
        identifiers: alpha_all
        match: |
          const selectedStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");
    name: widgetShellStyle
```

This matches a string literal AST node, not arbitrary source text. The pattern
uses Rust `regex` syntax against the decoded string literal value, so it is
unanchored unless you write `^` / `$`. Escape the regex as a JavaScript string
inside the selector source; for example, write `"\\d+"` when the regex should
see `\d+`.

Variable-length **list holes** absorb a contiguous run rather than a single
node. Their optional suffixes are labels for readability; they do not bind the
absorbed sequence for cross-occurrence equality.

- `ARGS` (or `ARGS_name`) in a call or `new` argument list matches any run of
  arguments, including none. Use it to pin stable arguments without spelling
  volatile generated siblings:

  ```js
  register("stable", ARGS_BEFORE, selectedValue, ARGS_AFTER);
  ```

- `STMT_LIST;` (or `STMT_LIST_name;`) in a block body matches any run of
  statements, including none. At top level it is supported in
  `anonymous_statements[].source_match` selectors that use `target_statement`
  or `target_statements`.
- `OBJECT_PROPS` (or `OBJECT_PROPS_name`) in an object literal matches any run
  of key/value properties or spreads. Anonymous `ANYTHING` in the same
  shorthand-property position is equivalent. Use either form to pin only the
  stable keys needed to make the selector unique, without overpinning generated
  sibling properties:

  ```js
  {
    requiredKey: EXPR,
    OBJECT_PROPS_GENERATED,
    anotherKey: EXPR,
  }
  ```

- `CLASS_REST;` as a class field (no initializer) matches a run of class
  members — "this class by these members, ignore the rest". `CLASS_REST` is an
  exact token (not a prefix).
- `DECLARATORS` (or `DECLARATORS_name = null`) inside one variable declaration
  matches a run of declarators.

```yaml
members:
  - selector:
      source_match:
        identifiers: alpha_all
        match: |
          class K {
            increment() {
              STMT_LIST_BODY;
            }
            CLASS_REST;
          }
    name: Counter
```

A list may take **several** holes. Each hole is a gap, and the members or
statements you pin between the holes are matched as an **ordered subsequence**:
in source order, each pinned run contiguous, with every hole absorbing an
arbitrary run (including none) of the candidate's elements. With no leading
hole the first pinned run is anchored to the candidate's start; with no
trailing hole the last pinned run is anchored to its end. So
`class K { a() { … } b() { … } CLASS_REST; }` matches a class whose **first
two** members are `a` then `b`, followed by anything, while
`class K { CLASS_REST; open() { … } CLASS_REST; close() { … } CLASS_REST; }`
matches any class with an `open` method somewhere before a `close` method.
Either way the match is ordered — it is _not_ an unordered "contains these
somewhere" match, and pinning `close` before `open` would not match a class
that defines `open` first. When more than one alignment is possible the
leftmost is used; that interior choice never changes _which_ declaration
matched, and a selector that matches more than one top-level declaration is
still a hard error.

For class fingerprints, keep method bodies as loose as the selector permits.
If a stable method name and order are the real anchors, put `STMT_LIST;` in the
method body and let `CLASS_REST;` absorb unrelated members. Spell concrete
statements inside an anchored method only when those statements are part of the
intended fingerprint; otherwise small upstream body drift will make the class
miss even though the method anchors are still present.

A hole works in **any** position — leading, middle, or trailing. Under
`alpha_all`, identifiers match by an alpha-correspondence the matcher builds as
it walks both trees, and a hole never contributes the identifiers it absorbs,
so the members or statements after a hole still match by their own structure
rather than by absolute position. (Single-node `EXPR`/`STMT` holes share this
property.)

## Workflow: renaming a binding without moving

```bash
debundle bindings rename XOe PluginSettingsAccessor
```

`<original>` accepts minified or current readable form. Validation is
name-collision detection (no two bindings share the same readable name
within the chunk). Mostly a convenience over `bindings assign` for the
rename-only case. `--no-verify` / `--dry-run` available.

## Workflow: fixing an atom-split rejection

When `bindings assign` rejects with an "atom split" diagnostic, the
realizability gate found that the requested move would split an
indivisible owner set. The diagnostic names the split atom, its owners,
and the destinations each member would land in.

1. **Read the diagnostic.** It names the atom, lists each owner's
   current and proposed module, and the `DepKind` causes (eager_use,
   rebind, sequenced, ...).
2. **Inspect the atom.** Run `debundle describe <atom-id>` for graph
   context (which owners are in it, why they're co-bound), then
   `debundle show-source <atom-id>` to read the source.
3. **List atoms broadly** if you need to triangulate:
   ```bash
   debundle atoms --format json | jq '.[] | select(...)'
   ```
   `debundle coverage` reports per-atom spec coverage; rows tagged
   "split" by current YAML are not landable until the partition agrees.
4. **Either expand the move set or revisit the partition.** The
   diagnostic does not auto-compute the minimal completion ("also move
   `rRe` and `MRe`"); read the printed atom membership and add the
   missing moves to your batch.
5. **Re-run** with the expanded batch.

The gate refuses; nothing on disk has changed.

## Peel heuristics: patterns that should move together

These patterns surface repeatedly when peeling minified bundles. Each
one usually appears as a singleton or two-statement `auto_partition_*`
shell whose source happens to live one line below a named binding;
the factorizer can't always see the textual adjacency, so the human
peeler (or a custom propose-side heuristic) makes the call.

### TypeScript decorator wirings live with the class they decorate

TypeScript / Babel emits class-field decorators as standalone
top-level statements right after the class declaration. The shape
varies by toolchain but the body is always
`__decorate([…decorators], <Class>.prototype, "<member>"[, <kind>])`
where the wrapping function name is minified (`t0`, `Q0`, `b0t`,
`__decorate`, etc.). They surface as anonymous statements in the
bundled output. Two reliable signals:

1. The first positional argument is an array literal of decorator
   references (`[Z]`, `[oe]`, `[ee, ie]`, …) — the bundler doesn't
   inline that array for any other call shape, so this is a
   high-precision fingerprint.
2. The second argument is `<Class>.prototype` where `<Class>` is a
   binding the spec already owns.

When both hold, the statement belongs in `<Class>`'s module via
`anonymous_statements:` — the chunker just happened to split it into a
sibling shell. Absorbing them costs zero realizability headroom (the
edges they carry already point at the class).

### Adjacent-ordinal singletons referencing each other

If owners at consecutive statement ordinals `N` and `N+1` are each
the other's only edge target/source, they were lines next to each
other in the source and the partitioner accidentally put them in
sibling shells. Merge the two `auto_partition_*` modules into one.
Detectable via `cluster(A).out == [B] && cluster(B).in == [A]` plus
`|N+1 - N| == 1`.

### `system_ids` + one heuristic

Some modules are _ambient_ — every other module touches them but
they don't constrain placement. When a singleton has exactly two
neighbors and one is an ambient module (e.g. `domains/system/ids`
holding chunk-wide enum constants, or `infra/vite/asset_map`
holding the chunker's dynamic-import lookup), treat the singleton
as effectively single-neighbor and absorb it into the _other_
named home.

### Lazy-loaded React wrappers fold into their consumer

`const X = b.lazy(() => bt(() => import("./<chunk>-<hash>.js")))` is
a one-liner. Its only non-`asset_map` neighbor is the module that
mounts `<X />`. Move `X` into that consumer's YAML.

### Commenting `anonymous_statements:` entries

Each `anonymous_statements:` entry accepts an optional `comment:`
**or** `note:` field — both are `Option<String>`, both are
preserved on round-trip. The materializer resolves statements from
`match:` and emits `comment:` immediately before the matched statement
in generated JS; `note:` remains YAML metadata. Graph-backed CLI
checks resolve the same selector against source and map the matched
statement back to the owner graph, so the spec stays selector-based.
Debundle's output is meant to turn minified compiled chunks into nice
human-readable code. Use emitted `comment:` text as part of that
readability surface: explain intent, invariants, and module
relationships. Keep provenance, owner IDs, and source-call trivia in
`note:` or omit them. Use `note:` for scratch reverse-engineering
notes that should survive debundle edits but should not appear in
generated JS, including uncertainty, provenance, and call-site
observations.

```yaml
anonymous_statements:
  - match: "Foo.prototype.bar = true;"
    comment: |
      Enables Foo.bar before consumers import Foo.
  - match: "registerFoo(foo);"
    note: "uncertain: looks like a registration side effect"
```

`comment:` is accepted on `anonymous_statements:` entries even
though the field originated at module and per-member level — the
spec rejects unknown fields otherwise, and authors who reach for
the familiar spelling shouldn't hit a cryptic parse error.

## Workflow: merging two modules

```bash
debundle modules merge --target <T> <source1> [<source2> ...]
```

Splices `members:` + `anonymous_statements:` from each source YAML
into `<T>`; creates `<T>` if needed, then deletes the source YAML
files.
Module-file args may include or omit `.yaml`, so `runtime/plugins`
and `runtime/plugins.yaml` name the same module file.

```bash
# Preview only — print verdict and planned diff summary.
debundle modules merge --dry-run --target runtime/plugins runtime/widgets

# Apply.
debundle modules merge --target runtime/plugins runtime/widgets
```

Source-module `comment:` content is concatenated into the target's
module-level `comment:` with a `--- from <source>:` divider when
sources have non-empty comments.

The realizability gate runs against the post-merge partition before
the YAML splice fires — pass `--graph <owner_graph.json>` so the
gate has the chunk's edge topology. The gate rejects merges that
would create cross-module cycles; `--dry-run` runs the gate without
writing. Use `--no-verify` to skip the gate (e.g. during multi-step
refactors where an intermediate state is intentionally invalid).

## Workflow: authoring `comment:` fields

Module YAMLs and `members:` entries carry the same optional
`comment:` field as `anonymous_statements:` entries (see above):

```yaml
# Module YAML
comment: |
  Coordinates foo registrations and lookup state.

members:
  - name: FooAccessor
    selector:
      binding: { name: a, kind: variable_declarator }
    comment: |
      Reads the active foo registry without mutating it.
```

Edit module and member comments via the CLI (assumes
`DEBUNDLE_MODULES` is exported; pass `--modules <dir>` otherwise):

```bash
# Set a member's comment from a positional arg.
debundle bindings comment a "Accessor for foo state."

# Open $EDITOR (fallback $VISUAL, then vi) pre-populated with the current comment.
debundle bindings comment a --edit

# Read the current comment (plain text on tty, JSON on pipe).
debundle bindings comment a

# Remove the comment entirely.
debundle bindings comment a --clear

# Same three modes for module-level comments.
debundle modules comment runtime/foo --edit
```

`<sym>` accepts minified or readable; `<module>` is the module path
relative to `$DEBUNDLE_MODULES`.

Move semantics (CLI surface, not a separate feature):

- `bindings assign` carries a member's `comment:` with the member as
  it moves between modules.
- `bindings assign` auto-deletes a drained source module only when its
  module-level `comment:` is empty/absent.
- `modules merge` concatenates source-module comments into the target.

CLI editing is live for module and member comments; anonymous
statement comments are authored directly in YAML. Module, member, and
anonymous-statement `comment:` fields emit into generated JS. `note:`
does not emit.

When a binding cannot yet be stabilized because the selector language lacks a
concise matcher, leave a member `comment:` naming the concrete matcher/tooling
blocker and the desired future feature instead of silently keeping minified
binding debt:

```yaml
comment: |
  blocked on Ducktape support for <specific matcher/tooling capability needed here>
```

Do not leave blocker comments for selector patterns Ducktape now supports, such
as matching one declarator inside a multi-declarator declaration or bracketing
object literal properties with `ANYTHING` / `OBJECT_PROPS`.

## Renaming or disabling a module

Not a CLI operation — plain `mv` on the YAML file:

```bash
# Rename: the module path is re-derived from the new filename.
mv $DEBUNDLE_MODULES/runtime/plugins.yaml $DEBUNDLE_MODULES/runtime/plugin_settings.yaml

# Disable: any non-.yaml suffix makes the spec compiler skip the file.
mv $DEBUNDLE_MODULES/runtime/plugins.yaml $DEBUNDLE_MODULES/runtime/plugins.yaml.disabled
```

The next mutating command (or `debundle run`) re-validates and
surfaces any resulting atom split as a gate diagnostic.

## Evidence files

Typical debundle outputs include:

- executable JS under `app/`
- root reports under `reports/`: `output.json`, `chunks.json`,
  `runtime.json`, `source_assets.json`, `provenance.json`,
  `rename_queue.json`, `vendor_swaps.json` when those outputs are configured
- per-chunk reports under `reports/tree/<chunk-id>/`: `chunk.json`,
  `modules.json`, `owner_graph.json`
- `reports/tree/<chunk-id>/cycles.json` or
  `reports/tree/<chunk-id>/atomic_unit_conflicts.json` only when
  validation rejects
- mirrored per-directory and per-file dependency reports under
  `reports/tree/**/index.json` and `reports/tree/**/*.js.json`

Use manifests for progress reporting rather than rescanning generated
JS by hand. Use tree reports for hierarchy-health evidence:
incoming/outgoing semantic dependency counts by kind, and full
symbol/file attribution for boundary crossings that make a directory
leaky or well-encapsulated. Treat them as graph evidence to pair with
source reading, not as a substitute for understanding the
implementation.

## Gate discipline

The adapter-provided gate is authoritative. For suspicious green
builds, force a fresh execution using whatever mechanism the project
build supports. Do not trust cache-only success when validating new
module boundaries.

Generated JS conflicts should be resolved by the adapter regen
command, not by hand-editing generated output.

## See also

- `cli.md` — current command surface.
- `README.md` — crate pitch, Bazel integration, `comment:` schema.
- `design.md` — the realizability theorem the gate enforces.
