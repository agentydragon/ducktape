# Ordered own-data field reads

## Missing shared operation

The shared provider executor could construct a record and update its existing
fields, but not read a field back at its source position. A neutral example is:

```js
function select(input) {
  const record = { value: input };
  const alias = record;
  return alias.value;
}
```

This body stores and returns an opaque input without invoking that input. Its
field read now uses the same literal heap and existing fresh own-data guard as
the ordered field-write operation. Source collection retains the static dot
read, its hygienic receiver binding and original member span. It does not
rescan an AST or infer a provider identity from a familiar helper shape.

## Proof and fallback boundaries

The central guard requires one settled ordinary record allocated in this
frame, absent from borrowed inputs, with an existing source-proved data field
and a current heap slot. It is unchanged for writes. The read copies the slot
at that operation's prefix; weak allocation-site updates, aliases and
self-links keep their existing representation. This adds no generation or
heap-locality premise. Reused sites, arrays, unknown/missing fields, accessors
and borrowed inputs cannot supply this strict proof.

The application adapter reuses the canonical member-read call constructor and
normal effect transfer for other receivers. Its established native, array,
enum and borrowed-data proofs remain available. This path receives only a
simple declarative-cell receiver; it introduces no replay of an effectful
receiver expression. When the normal read transfer completes, its value is
selected through the existing value transfer. Its allocation judgment remains opaque.
If getter/callback behavior remains unproved, retain the read's effects and
use the existing bounded callback closure to summarize the remaining body.
The frame retains entry capabilities, external values read during execution,
current locals and fresh records even after a local alias is replaced. It
does not retry the earlier operations or treat a complete outcome as a proof.

The strict provider adapter refuses an unproved read. It cannot read a
symbolic argument's properties, use application-only assumptions or retry the
legacy AST evaluator after refusing. A returned slot value can be a scoped
argument, capture, primitive or fresh record under the existing result and
retention rules. Its later properties and callable body remain unproved.

Only direct dot reads from simple bindings enter this slice. Computed,
optional and nested member expressions, calls and control flow remain outside
the ordered grammar. Real CommonJS loader caches, live captured cells, facade
getters and actual export-property identity still need their shared semantic
transfers. This change alone does not admit the audited React provider or
clear a canonical gate.

## Review and encountered friction

Reviewed the canonical lowering, free-read cache, both context adapters,
unchanged own-data guard, member call construction, callback fallback and
strict result/retention projection. No tests or local build/corpus/profiling
jobs were added or run. Hosted PR CI supplies compilation and existing checks.

**F13 — Explain the ordered field-proof refusal:** addressed. The shared guard
returns its first typed refusal condition, and a conservative application
`member_read` carries that reason on the existing precision-loss row at the
original source span. Reasons distinguish receiver shape, borrowed receiver,
unsupported record shape, allocation not proven fresh, unsettled properties,
missing source field, non-data descriptor and missing heap value. The freshness
label does not claim a distinct runtime allocation. This is diagnostic only;
the strict provider still refuses the same operation and the write guard is
unchanged.
