# Purity classification — soundness notes

Admission rules and long-form rationales for the whitelist tables and
`ChunkBinding::PlainData` in `devinfra/js/debundle/purity/`. The code holds
3–5 line summaries; the exhaustive prose lives here so future audits don't
have to scroll past 80 lines of `///` to read the next function. Read
<design.md> § "Soundness over completeness" first — it is the contract
every rule below serves.

## Admission rule: `PURE_STATIC_CALLS`

`PURE_STATIC_PROPS`, `PURE_STATIC_CALLS`, and `PURE_GLOBAL_CALLS` in
<../purity/whitelists.rs> directly drive S-edge construction. An
over-approximating entry — a built-in flagged Pure that can in fact fire
user code on some argument — drops S edges the realizability theorem needs,
and can let a cyclic spec slip past the validator and emit a bundle whose
runtime ordering breaks.

A new entry is admissible only when:

- The operation fires **no user-defined code on any argument type**: no
  `ToNumber` / `ToString` / `ToPrimitive` / `ToPropertyKey` coercion of the
  args (these fire `valueOf` / `toString` / `[Symbol.toPrimitive]`); no
  iterator protocol (`[Symbol.iterator]`); no proxy traps; no own-property
  `[[Get]]` / `[[Set]]` (these fire user accessors); no mutation of any
  reachable object; no `toJSON` callback path.
- It cites the ECMA-262 clause that establishes this. "Common in production
  bundles" is not a soundness argument, and neither is "often called with a
  fresh-literal target or primitive args in practice" — the validator does
  not infer types and cannot rely on argument shape unless it is enforced
  syntactically at the call site.

If the whitelist proves too restrictive in practice, that is the expected
failure mode. Loosen it only by adding universally-pure operations, or by
introducing stronger argument analysis (e.g. a statically-primitive
classification) that admits a wider set _under syntactic gates on the
arguments_. Never add an entry because the common case is fine. The
analysis engine carries an inline TODO listing patterns that become
admissible once a primitive-arg gate exists — that is the queue for safe
growth.

Every admission argument here additionally assumes **intrinsic integrity**
(<design.md> A11): the chunk runs with unmodified built-in prototypes.
Prototype pollution — replacing `Set.prototype.add` or
`Array.prototype[Symbol.iterator]` — defeats every entry whose ECMA-262
citation describes the standard built-in, and the analyzer cannot detect it
because the pollution may live outside the analyzed chunk.

## `PURE_STATIC_FUNCTION_REFS` — reads vs calls

This table whitelists static-property _reads_ of function-valued built-ins
(`const define = Object.defineProperty;`). Reading such a property fires no
getter per ECMA-262, so the read itself is pure. The **call** of the
resolved value remains subject to the `PURE_STATIC_CALLS` contract above:
call shapes that mutate, fire iterators, fire user getters, or invoke proxy
traps must not be added to `PURE_STATIC_CALLS` regardless of whether the
read appears here.

Every entry lands with both directions pinned:

- a positive `static_function_ref_*_alias_is_pure` test (`Receiver.method`
  classifies as `Pure`), and
- a negative `static_function_ref_*_calls_remain_unknown` test
  (`Receiver.method(args)` classifies as `Unknown`).

The pairing prevents a future maintainer from misreading a read-pure entry
as call-pure and promoting it into `PURE_STATIC_CALLS`.

## Declared purity — spec trust contracts

The spec format admits optional per-member `purity:` annotations. With
`purity: "pure"` the validator treats calls to the bound Ident as `Pure`
regardless of the function body; with `purity: "pure_new"` it treats
`new BoundIdent(...)` as `Pure` when every constructor argument is pure.
The validator does **not** re-verify the body or constructor — an incorrect
annotation produces a buggy debundle exactly as an incorrect spec selector
does. Soundness shifts to the spec author.

Use an annotation when the body is too dynamic for static inference
(dynamic dispatch through a registry, dynamic property access), when it
calls vendor code outside the chunk's analyzable scope, or when the
function is opaque by construction (a host-provided callback, an FFI shim)
but is author-known to have no observable side effects.

`pure` overrides A8's shadowing fallback: a chunk that imports `Boolean`
from userland _and_ declares the local `Boolean` binding pure uses the
declared-pure path — the author asserts THIS bound value is pure, wherever
it came from. Arguments are still evaluated normally; declared purity
covers the function value, not its arguments. `pure_new` is separate from
`pure`: it covers only `new X(...)`, does not make `X(...)` pure, and still
evaluates constructor arguments normally.

`pure_members: [<prop>, …]` is a third trust contract on the same member,
extending "calls of the bound Ident are pure" to "calls of
`<binding>.<prop>(args)` for each listed property are pure when args
evaluate pure". The intended shape is a vendor namespace binding — a
star-import or renamed binding standing in for a vendor module such as
React — whose member function values are author-known side-effect-free even
though the analyzer cannot inspect them.

```yaml
- name: React
  selector:
    binding: { name: b, kind: import_specifier }
  pure_members: [forwardRef, lazy, memo, createContext]
```

Only static identifier-property access fires the rule
(`<binding>.<prop>(args)` / `<binding>?.<prop>(args)`). Computed access
(`<binding>[expr](args)`), chained access (`<binding>.x.y(args)`), private
fields, and non-Ident receivers fall back to the regular classifier path.
As with `purity: pure`, the rule wins over the global-shadowing fallback,
and args are still classified independently. The annotation is
**per-member**: a sibling member without it stays subject to inferred
classification. There is no "declare a whole module pure" shorthand.

## `ChunkBinding::PlainData`

A chunk-top binding admits as `PlainData` when its declared value is a
plain object/array literal, and the chunk-wide write scan can prove no
post-init mutation can install an accessor on the binding's cell.
Concrete shapes admitted: `const X = <plain literal>`; `let X = <plain
literal>` / `var X = <plain literal>` with every chunk-top re-bind
being another plain literal (whole-object replacement, no in-place
mutation); the TS-enum-IIFE shape `var X = ((p) => (p.A = "a", …, p))(X
|| {})` whose IIFE produces a plain object by parameter mutation (see
`is_ts_enum_iife_init_for_binding`).

Why member reads on PlainData are pure:

- **No accessor channels at any program point.** The initial init is a
  syntactic plain literal — no getters/setters/methods/computed
  keys/`__proto__` — so the value carries only data properties. Every
  re-bind writes another plain-literal value, so the post-write value
  still has only data properties. Object/array spread inside the
  literal (`{...src}`, `[...src]`) is permitted: `CopyDataProperties`
  copies values via `CreateDataPropertyOrThrow` regardless of the
  source's descriptor shape — the result is plain.

- **No post-init accessor installation.** The chunk-wide write scan
  rejects any member write (`X.k = …`, `X[k] = …`), member update,
  member delete, or call to `Object.{defineProperty,defineProperties,
setPrototypeOf,assign}` /
  `Reflect.{defineProperty,set,setPrototypeOf,deleteProperty}` with
  `X` as first arg. Plain-ident writes whose RHS is not a
  plain-literal also disqualify.

- **No escaping references.** The write scan above is name-based: it
  only catches mutations spelled through the candidate's own
  identifier. An alias (`const Y = X; Object.defineProperty(Y, …)`)
  or any captured reference (call argument, array element, object
  property value) would defeat it, so the scan also treats every
  bare candidate `Ident` outside a short list of provably
  non-capturing read positions (member receiver, spread source,
  `typeof`/`!`/`void` operand, `Object.{keys,values,entries,freeze,
fromEntries}` single argument with `Object` unshadowed, the vetted
  TS-enum-IIFE argument, `return X` / `() => X`, `export { X }`) as
  an escape and disqualifies the candidate.

  **Residual assumption for `return X` / `export { X }`:** consumers
  of a returned or exported PlainData reference (chunk-internal
  callers of the returning function; importers of the exported
  binding in other modules of the same debundle) are assumed not to
  install accessors on it. For exports this is the same single-bundle
  closed-world view the rest of the analysis takes; for returns it
  admits the ubiquitous accessor pattern `const get = () => CONFIG;`
  without a whole-program escape analysis. A consumer calling
  `Object.defineProperty` on the received reference would break the
  read-purity claim — the same way a chunk-external mutation already
  could (the scan only ever sees one chunk).

- **Re-bind soundness for `let`.** Reads on `X` after a re-bind see
  the new plain-literal value; reads before still saw the old
  plain-literal value. At no program point does `X` hold an
  accessor-carrying value, so member reads fire no user code regardless
  of write/read interleaving. The re-bind statement itself is
  independently classified impure (the existing `AssignOrUpdate` rule
  on `Expr::Assign`), so it keeps its `S`-edge and the debundler
  cannot move it past sequenced points.

Sound enabler for the recursive-purity claim "`x` reads on `x.k` /
`x[pure]` are pure iff `x` is bound to a plain-data shape that has no
accessor channels". Eliminates the chain-of-hints needed when a
chunk-local helper's body is just a property read on a chunk-local
config object — including the `let envConfig = {...}` / `envConfig =
{...envConfig, ...n}` pattern that the
`runtime/environment/env_config.yaml` spec hints in gaffer-private
were a workaround for.

## `PURE_OBJECT_CALLS_ON_PLAIN_DATA`

Static `Object` methods that are Pure when called with a single
argument that is structurally a fresh plain-data object/array literal
(no accessors / methods / `__proto__` / computed keys / spread of
non-plain-data sources) OR a chunk-top binding that has admitted as
`ChunkBinding::PlainData`.

The contract is stricter than `PURE_STATIC_CALLS` because every member
here either invokes `[[Get]]` on own keys (which fires user getters /
Proxy traps on a general argument) or mutates descriptor state
(`freeze`). Restricting the argument shape to a fresh plain-data
receiver — verified syntactically at the call site — closes both
holes: no own-key access can fire a user accessor (none exist), and
the mutation in `freeze`'s case targets a value that is not aliased
through any user-observable channel before the call.

The general-arg form of these calls stays in `PURE_STATIC_FUNCTION_REFS`
(read-pure, call-unknown), matching the rule that we cannot admit `[[Get]]`
or descriptor mutation on an arbitrary receiver. New entries need the same
per-entry ECMA-262 citation as `PURE_STATIC_CALLS`, plus paired tests in
`purity/classifier_tests.rs`: a positive
`object_<prop>_on_plain_<shape>_classifies_pure` (`Object.<prop>({lit})` is
Pure) and a negative `object_<prop>_on_<non-plain>_stays_unknown`
(`Object.<prop>(somefn())` and the opaque-binding form are Unknown).
Cycle-breaking behaviour is pinned in <../e2e/object_plain_data_calls_test.rs>.

Soundness per entry:

- `Object.keys(O)` — ECMA-262 §20.1.2.17 calls `ToObject(O)` (no
  coercion on an object), then `EnumerableOwnPropertyNames(O, "key")`.
  The latter calls `[[OwnPropertyKeys]]` (for an ordinary plain
  object: returns the integer-index keys then string keys in insertion
  order, no user code) and per-key `[[GetOwnProperty]]` to check
  `[[Enumerable]]` — also a structural read with no user code on a
  fresh plain literal / PlainData binding.

- `Object.values(O)` / `Object.entries(O)` — same as `keys` but
  additionally call `[[Get]]` on each own key. For an ordinary
  plain-data receiver every own key resolves to a data property
  ($\Rightarrow$ no accessor fires). PlainData receivers carry the
  same guarantee by the chunk-wide write scan (`PlainDataWriteScanner`
  rejects any `Object.defineProperty(X, …)` /
  `Object.setPrototypeOf(X, …)` that could install an accessor
  post-init).

- `Object.freeze(O)` — ECMA-262 §20.1.2.6 calls
  `SetIntegrityLevel(O, "frozen")` which sets `[[Extensible]]` to
  false and rewrites each own property descriptor to non-configurable
  (and non-writable for data properties). No `[[Get]]` is performed,
  no user code fires. The mutation is on the just-allocated literal
  (for the literal form) or on a binding whose only producer/consumer
  is the chunk being debundled (for the PlainData form), so it cannot
  perturb user-observable state outside the call.

- `Object.fromEntries(I)` — ECMA-262 §20.1.2.7 invokes
  `I[@@iterator]()`. For a fresh `Array` literal with no spread, that
  resolves to the built-in Array iterator, which `[[Get]]`s indices
  `0..length` (own data properties on a fresh array, no user code) and
  stops. Each yielded entry must itself be a 2-element Array literal
  whose `[0]/[1]` reads are own data properties (gated by
  `is_fresh_entry_array_for_from_entries`). Both gates together rule
  out the "non-iterable argument throws TypeError" path that breaks
  purity for arbitrary arg shapes (the throw is observable). For a
  PlainData Object binding the call would throw, so PlainData shapes
  are admitted only for the non-fromEntries methods.

Out of scope (not admitted; flagged for follow-up review):

- `Object.assign(target, src)` — mutates `target` AND calls
  `[[OwnPropertyKeys]]`/`[[Get]]` on `src`. The target-mutation half
  rules out the literal-arg shortcut: even with two literal args,
  `Object.assign({}, …)` returns the first arg mutated, which is
  observable only if the result is captured — but the mutation itself
  is invisible without the capture, so this is safely pure-of-result.
  Skipped to keep the rule tight; the `assign` path needs its own
  argument-count + result-shape analysis.

- `Object.getOwnPropertyNames(O)` / `getPrototypeOf(O)` /
  `getOwnPropertyDescriptor(O, k)` — same shape as `keys` but produces
  a richer return value. Could ride on the same gate in a follow-up;
  out for v1 to minimize the audit surface.

- `Array.from(I[, mapFn])` — sound only when `mapFn` is absent (a
  `mapFn` invokes user code per element). Skipped to avoid the
  per-call argument-count gate.
