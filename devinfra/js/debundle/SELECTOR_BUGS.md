# Selector Bugs And Gaps

This note tracks selector issues found while porting a downstream debundle spec.
Examples are intentionally generic and anonymized.

## Global Matching Needs Injective Target Assignment

Some selectors are intentionally broad, and the spec relies on the whole
assignment to disambiguate them. Solving each selector independently, or
dropping target injectivity, leaves those broad selectors ambiguous.

Observed pattern:

```js
const broadCandidate = f(134);
const exactCandidate = f(123);
```

Given two readable claims:

- `X`: `const x = f(ANYTHING);`
- `Y`: `const y = f(123);`

`Y` should bind to `exactCandidate`, and target injectivity should force `X` to
`broadCandidate`. Without a native/global `all_different`, both `X` and `Y` can
claim `exactCandidate`, or `X` can remain ambiguous between both candidates.

Desired behavior:

- Treat `all_different` across claimed targets as selector semantics, not as a
  best-effort diagnostic.
- Enforce it in the exact-assignment backend so forced matches propagate to
  broader selectors instead of enumerating invalid duplicate rows and rejecting
  them late.
- Add regression coverage before replacing the current `AssignmentRow` solver.

## Stable Identifiers Are Only Local To One Match

`source_match` with `identifiers: alpha_all` makes readable names usable for
bindings that are local to the selected AST. It does not make those names a
global aliasing layer across independently matched selectors.

Observed failure mode:

```js
function readContext(node, limit = contextLimit) {
  return renderNode(node).slice(0, limit);
}

async function produceResult(node, services) {
  const context = readContext(node, contextLimit);
  if (context.length < minContextChars) return [];
  return services.run(context);
}
```

If `readContext`, `contextLimit`, and `minContextChars` are captured by other
selectors, a separate selector for `produceResult` that mentions those readable
names can fail to match. In the split selector, those names are free references,
not local binders. The matcher has no stable cross-selector environment saying
that `readContext` means the already selected minified binding.

Workarounds:

- Put related declarations in one binding group when they are actually adjacent.
- Replace cross-selector references with `EXPR_*` holes.
- Fall back to raw binding selectors for large functions when grouping is not
  possible.

Desired behavior:

- Either support a selector environment where previously exported readable names
  can be referenced from later `source_match` selectors, or produce an error that
  explains that free readable identifiers are not resolvable.

## Multi-Declaration Source Matches Are Hard To Use

Binding groups are useful for capturing multiple names from one stable selector,
but source matches spanning more than one top-level declaration are fragile when
the source has unrelated declarations between the anchors.

Observed failure mode:

```js
const configLimit = 100,
  minInputChars = 10,
  resultLimit = 15;

function helperA(input) {
  return input.slice(0, configLimit);
}

function helperB(input) {
  return helperA(input);
}
```

A single binding group that tries to capture the constants and both functions
fails if unrelated declarations sit between the constant run and function run.
The error is reported as "target_binding did not match any top-level
declaration", but it does not identify which part of the multi-declaration
selector failed.

Desired behavior:

- Support non-contiguous selector groups, or document that binding-group
  `source_match` only matches contiguous top-level items.
- Improve diagnostics by showing the first unmatched selector item and whether
  the candidate was rejected by shape prefiltering or by binding comparison.

## Adjacent Binding Groups Can Miss Large Alpha-Renamed Functions

A binding group over adjacent top-level declarations can still miss when a
readable `alpha_all` selector targets a large function with many local
renamings and helper calls.

Observed pattern:

```js
const textDecoder = new TextDecoder();
function decodeRecord(buffer) {
  let offset = 0,
    name,
    children;
  const fieldCount = readMapLength(buffer, offset);
  offset += readPrefixLength(buffer[offset]);
  for (let fieldIndex = 0; fieldIndex < fieldCount; fieldIndex++) {
    const keyLength = readStringLength(buffer, offset);
    offset += readPrefixLength(buffer[offset]);
    const key = decodeString(buffer, offset, keyLength);
    if (((offset += keyLength), key === "props")) {
      const propCount = readMapLength(buffer, offset);
      offset += readPrefixLength(buffer[offset]);
      for (let propIndex = 0; propIndex < propCount; propIndex++) {
        const propKeyLength = readStringLength(buffer, offset);
        offset += readPrefixLength(buffer[offset]);
        const propKey = decodeString(buffer, offset, propKeyLength);
        if (((offset += propKeyLength), propKey === "name")) {
          const nameLength = readStringLength(buffer, offset);
          ((offset += readPrefixLength(buffer[offset])),
            (name = decodeString(buffer, offset, nameLength)),
            (offset += nameLength));
        } else offset = skipValue(buffer, offset);
      }
    } else if (key === "children") {
      const childCount = readArrayLength(buffer, offset);
      ((offset += readPrefixLength(buffer[offset])), (children = new Array(childCount)));
      for (let childIndex = 0; childIndex < childCount; childIndex++) {
        const childLength = readStringLength(buffer, offset);
        ((offset += readPrefixLength(buffer[offset])),
          (children[childIndex] = decodeString(buffer, offset, childLength)),
          (offset += childLength));
      }
    } else offset = skipValue(buffer, offset);
    if (name !== void 0 && children !== void 0) break;
  }
  return [name, children];
}
```

The declarations were adjacent and the literals/shape were distinctive, but
the binding group reported a generic no-match for the function target. A raw
binding fallback was needed for the large function, while a smaller neighboring
helper remained expressible as a structural selector.

Desired behavior:

- Make adjacent binding groups with function declarations as reliable as
  multi-declarator groups under `alpha_all`.
- Add diagnostics for the first incompatible local binding or expression shape
  when a large alpha-renamed function selector fails.

## Declarator-List Holes Need Better Diagnostics

`DECLARATORS_*` holes are useful for matching runs of variable declarators, but
failures can be opaque when the hole placement is too broad or insufficiently
anchored.

Observed pattern:

```js
const DECLARATORS_BEFORE = null,
  wantedA = 1,
  wantedB = 2,
  DECLARATORS_AFTER = null;
```

This is attractive when only two declarators in a long `const` run matter, but a
miss looks the same as any other `source_match` miss. It is not obvious whether
the list hole was unsupported by the selected Ducktape binary, was too greedy,
or failed because the anchored declarators did not match.

Desired behavior:

- Make list-hole support explicit in diagnostics when a selector uses
  `DECLARATORS_*`.
- Report list-hole binding spans during verbose/debug selector matching.
- Consider a safer syntax for "capture these declarators from a larger run" that
  does not require fake `= null` declarators.

## Alpha-Renamed Multi-Declarator Groups Can Miss

A binding group over one `const` declaration matched when written with emitted
local names, but missed when the same selector was written with readable names
under `identifiers: alpha_all`.

Observed pattern:

```js
const applyOverrides = (patch) => {
    config = { ...config, ...patch };
  },
  readConfig = (key) => config[key],
  isLocalMode = () => (runtime.LOCAL ? true : readConfig("ENV") === "local"),
  isReplayMode = () => readConfig("REPLAY") === "true",
  snapshotConfig = () => config;
```

The same shape using the emitted binding names matched and exported all five
bindings. This suggests either alpha matching is not consistently applied across
some multi-declarator arrow-function runs, or diagnostics hide a more specific
sub-expression mismatch.

Desired behavior:

- Make alpha-renamed multi-declarator groups work consistently with individual
  source matches.
- When alpha matching rejects a candidate, report the first incompatible
  identifier or property binding instead of a generic no-match.

## Async Destructured Function Selectors Can Miss

An `async function` with an object-pattern parameter failed to match even after
the selector used explicit property bindings rather than shorthand.

Observed pattern:

```js
async function runWithState({ itemPath: itemPath, items: items, promise: promise, state: state }) {
  const states = [getState(toKey(itemPath))];
  for (const item of items || []) states.push(getState(item.id));
  action(() => {
    states.forEach((entry) => {
      entry.state = state;
    });
  });
  try {
    await promise;
  } finally {
    action(() => {
      states.forEach((entry) => {
        entry.state = void 0;
      });
    });
  }
}
```

The target function was identifiable by raw binding name, but the structured
selector missed. This may be the same object-pattern limitation as shorthand
destructuring misses, or a separate async-function matching issue.

Desired behavior:

- Treat shorthand and explicit object-pattern bindings equivalently where they
  bind the same locals.
- Make async function declarations with destructured parameters match under
  `source_match`.
- Include parameter-pattern mismatch details in no-match diagnostics.

## Statement-List Holes Were Missing

Status: addressed by `STMT_LIST_*` holes for block/function statement lists.

Before this was added, `STMT_*` holes matched one statement and did not
represent zero or more statements.

Observed desired selector:

```js
async function runTask(input) {
  STMT_LIST_SETUP;
  try {
    STMT_LIST_BODY;
  } catch (error) {
    STMT_HANDLE_ERROR;
  }
}
```

Today this has to be rewritten as several single-statement holes, and it fails
when the number of setup/body statements changes.

Implemented behavior:

- `STMT_LIST_*` holes match zero or more statements in block/function statement
  lists.
- Top-level module-item list holes remain a separate possible extension.

## Pinned Binary Versus Local Selector Syntax

Downstream builds can accidentally run with an older pinned debundler even when
the module is overridden to local Ducktape sources. New selector syntax then
fails as a generic no-match rather than as an unsupported-hole error.

Desired behavior:

- Add a spec/schema feature version or selector capability check.
- When a selector contains an unknown hole shape, fail with "unsupported selector
  hole" rather than attempting to match it as ordinary JavaScript.

## Duplicate Claims Should Use Declaration Identity

Observed failure mode:

```js
const config = (() => {
  const n = 15;
  return { limit: n };
})();

function n(input) {
  return input.remove();
}
```

Two selectors can legitimately target different declarations that minify to the
same short spelling. A constant selector and a function selector should not
collide if they resolve to different AST bindings in different scopes.

Desired behavior:

- Track binding claims by declaration identity, not just emitted/minified
  spelling.
- Include declaration kind and source location in duplicate-claim diagnostics.
  The current duplicate message is hard to evaluate when minified names are
  reused.

## Standalone Selector Probes Rebuild Full-Chunk Domains

Observed selector:

```js
function decodeEntry(bytes) {
  let offset = 0,
    label,
    children;
  STMT_LIST_BODY;
  return [label, children];
}
```

This selector shape is attractive for forward compatibility: it pins the
surrounding declaration, initial locals, and return tuple while allowing the
parser body to drift. A current `spec match-selector` probe over a 7.14 MB
downstream chunk found its unique target but took 19.5 seconds and peaked at
1.56 GB RSS. A more specific version of the selector was slower, not faster, so
the statement-list hole itself is not the demonstrated bottleneck.

The solver build summary localizes the cost before search. The probe extracted
3.13 million facts for 1.19 million AST nodes. Two variables retained domains
containing every AST node, producing a 5.19 MB CP-SAT request with 2.38 million
domain values even though the lowered structural constraints admitted only two
allowed rows. Model construction alone took 10.9 seconds: 7.21 seconds building
fact domains, 2.10 seconds lowering atoms, and 1.11 seconds simplifying allowed
tuples. See
<debug/perf/2026_07_13_match_selector_full_domain_profile.md> for the command,
measurements, and interpretation.

Desired behavior:

- Slice selector variables to query-local candidate domains before serializing a
  backend request. Fixed declaration kind, literals, tuple arity, and list-hole
  anchors should prevent AST variables from retaining the full-node domain.
- Reuse the parsed chunk, fact store, and candidate indexes when evaluating more
  than one selector. The standalone probe should make one-selector cost visible,
  but the production pipeline must not pay full-chunk setup once per selector.
- Keep the existing phase summary and add a regression workload that asserts
  domain cardinality and request size as well as elapsed time. A time budget by
  itself would report the regression without preventing it.
