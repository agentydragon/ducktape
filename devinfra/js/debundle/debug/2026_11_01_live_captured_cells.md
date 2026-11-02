# Live captured lexical cells

## Missing behavior

An immutable snapshot of a closure's environment cannot represent later writes
to the same lexical binding. A neutral example is:

```js
function factory(first, second) {
  let selected = first;
  const read = () => selected;
  selected = second;
  return read;
}
```

The returned closure must observe the later store. Two calls to `factory` need
different `selected` cells even with equal arguments. Returning another closure
through an intermediate closure must retain the outer cell too. These are
source semantics, not a certificate that a bundler-shaped loader is pure.

## Implementation boundary

The canonical nested identifier census identifies bindings captured from
ordered bodies, including references after suspension. Only those bindings get
cells. Temporary owner lookup borrows declaration keys; no extra AST traversal
or full per-call read census is retained. Each ordered activation allocates new
monotonic cell IDs. Closure descriptions retain those IDs, and local stores
update their current values. Capture interning remains structural and is not a
runtime object-generation proof.

The application adapter accepts anonymous function/arrow creation and simple
captured assignments. The shared operation kernel preserves operand order and
TDZ/const errors. Nested captured assignments weakly join values because the
general invocation caller can include skipped calls. This does not yet prove
exact CommonJS initialization/cache transitions. Unsupported source paths
invalidate mutable-cell precision before using conservative summaries; a failed
ordered body never replays its prefix. The strict provider consumer declines
the new operations and gains no new certificate or premise.

Reachability and publication traverse current cell contents, retaining known
capabilities while unknown execution adds opaque alternatives. A skipped
continuation can initialize a lexical declaration, so invalidation preserves
both abrupt TDZ and completing possibilities. A known initialized const binding
keeps its identity; referenced objects still undergo normal heap invalidation.
New cells and changed contents version invocation memo keys. Cell references
are retained through intermediate closures without snapshotting their values.

This changes internal runtime types and all their consumers together. It adds
no serialized format, compatibility reader or prior-format support. Getter and
callback premises are unchanged. Named function-expression self bindings,
recursive ordered literal construction, branch/short-circuit execution,
member/native call results and property-origin export identity still require
work. Existing source-summary lanes remain conservative until those semantic
operations are migrated into the common executor.

## Review and friction

Reviewed source-binding ownership, activation identity, TDZ, strong local stores
versus weak captured stores, intermediate/deferred retention, escaped callback
reachability, prefix revisions and the provider-admission boundary. No tests,
builds, corpus runs, benchmarks or profiling jobs were added or run locally.
Normal hosted PR CI supplies existing checks; canonical provider acceptance has
not been established by this patch.

**F16 — Retained cell payloads:** the initial implementation retained cells and
interned capture descriptions until chunk completion, even when an activation's
closures were no longer live. Weak capture interning now releases descriptions
that have no actual or semantic owner; revoked audited-result descriptions stay
rooted. Owner-boundary collection now traces the live roots after invocation
memo retirement and reclaims unreachable cell payloads while keeping IDs
monotonic. Its boundary and root inventory are recorded in
<2026_11_01_retired_lexical_cells.md>. The per-statement invocation budget still
does not bound whole-chunk retained memory: live roots, conservative heap tables
and empty identity slots remain. No measured memory saving is claimed.
