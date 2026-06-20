# Selector Bugs And Gaps

This note tracks selector issues found while porting a downstream debundle spec.
Examples are intentionally generic and anonymized.

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

## Name-Pin Conversion Gaps Found Stabilizing The Spec

The following gaps block converting fragile minified-name pins
(`binding.name`) into stable `source_match` selectors. Each is a shape that
recurs in real entities but cannot be expressed today, so the entity stays
name-pinned.

### Arrow Whose Body Is A Parenthesized Object Literal

A concise arrow that returns an object literal — `() => ({ ... })` — cannot be
written as a selector. The selector grammar reads the leading `{` of the body
as a block statement, not as a parenthesized object expression, so the object
fields are never parsed as an expression.

Observed pattern:

```js
const makeWidget = (props) => ({
  kind: "widget",
  render: () => props.label,
  dispose() {},
});
```

This is the idiomatic shape for components/factories defined as
`const X = (props) => ({ ...stable copy string... })`, where the returned object
is a distinctive, re-minify-stable anchor. Because the body cannot be parsed as
an object, the function can only be pinned by its minified name.

Desired capability:

- Parse a parenthesized arrow body (`=> ( expr )`) as an expression body, so an
  object-literal-returning arrow is expressible and its object fields are
  available as anchors.

### Parenthesized Sequence Or Assignment Expression Body

A `return` (or arrow body) of a parenthesized sequence expression loses its
inner parentheses in the `source_match` JS parser, so the assignment inside it
cannot be matched structurally.

Observed patterns:

```js
function decorate(target, decorators) {
  return (applyDecorators(target, decorators), target);
}

function getSingleton() {
  return (instance || (instance = build()), instance);
}
```

The first is the esbuild/TypeScript decorate-helper shape; the second is the
memoized-singleton (lazy accessor) idiom. Both rely on the parenthesized
`(a, b)` / `(a || (a = b()), a)` sequence as their distinctive body, but the
inner assignment parens are dropped, so the selector cannot assert the structure
and the helper/accessor stays name-pinned.

Desired capability:

- Preserve parenthesized sequence/assignment expressions in the selector parser
  so `(seq, expr)` and `(a || (a = b()), a)` bodies match structurally.

### No Array-Element-Run Hole

There are run holes for object-property runs (`OBJECT_PROPS`) and class-member
runs (`CLASS_REST`), but no analogous hole for a run of **array elements**. A
long array-literal initializer can therefore only be pinned by spelling the
whole array, which over-pins on every element's incidental content.

Observed patterns:

```js
const catalog = [
  /* ~50 distinct entries */
];

const withExtras = [...base, EXTRA];
```

A ~50-entry catalog or a `[...spread, X]` initializer has no way to anchor on
the one or two elements that matter while leaving the rest as a hole.

Desired capability:

- An `ARRAY_ELEMENTS` / `ELEMENTS` run hole that matches zero or more array
  elements, so an array initializer can be anchored on its stable elements
  without spelling the whole literal.

### Comma-List Sibling Disambiguation By Nested Body

Same-arity declarators in one `const a = …, b = …` comma-list that differ only
in a deeply-nested property or value cannot be disambiguated: the matcher
reports both declarators as candidates.

Observed pattern:

```js
const handlerA = makeHandler({ route: { method: "GET" } }),
  handlerB = makeHandler({ route: { method: "POST" } });
```

Both declarators have identical shape and arity; they differ only in the nested
`method` value. A declarator-run selector matches both, so neither can be pinned
structurally.

Desired capability:

- A per-declarator nested-equality constraint, or a single-declarator window
  that can assert a nested anchor, so two same-arity siblings are distinguished
  by a deeply-nested property/value.

### No Non-Emitting Member Annotation

There is no way to attach a reviewer-facing reason to a member pin without
changing the generated JS. Member `comment:` **emits** into the generated
output (breaking a byte-identical gate unless the JS snapshot is regenerated),
and `note:` is rejected by the spec parser (the valid member keys are `name`,
`selector`, `purity`, `effect`, `pure_members`, `no_sync_callback_members`,
`comment`). A pin left as debt — e.g. one blocked by a gap above — therefore
cannot carry an explanation of why it is still a name pin.

Desired capability:

- A non-emitting member annotation field for reviewer-facing rationale, or make
  a blocker `comment:` non-emitting, so left-as-debt pins can record why without
  altering output.
