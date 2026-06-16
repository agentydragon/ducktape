//! Static purity-whitelist tables for the expression classifier.
//!
//! Each constant is a `&[...]` of known-pure operations keyed by
//! (receiver, property) or bare name. New entries land only with a spec
//! citation showing no user-callback path; "common in practice" is not
//! sufficient.

use std::collections::BTreeSet;
use std::sync::LazyLock;

/// Builtins that can install an accessor or rewire the prototype chain
/// of their first argument. Any candidate appearing as the first
/// positional arg of one of these calls is disqualified from
/// PlainData status. `Object.assign(X, ...)` writes data properties
/// to X but doesn't install accessors; conservatively included so the
/// rule is "X must not be written through, period" without per-builtin
/// reasoning about which kinds of properties end up on X.
pub(crate) const PLAIN_DATA_HOSTILE_BUILTINS: &[(&str, &str)] = &[
    ("Object", "defineProperty"),
    ("Object", "defineProperties"),
    ("Object", "setPrototypeOf"),
    ("Object", "assign"),
    ("Reflect", "defineProperty"),
    ("Reflect", "set"),
    ("Reflect", "setPrototypeOf"),
    ("Reflect", "deleteProperty"),
];

/// Static-property reads on these globals are Pure (no
/// observable side effect, no getter to fire). Indexed as
/// `(receiver_ident, property_name)`.
pub(crate) const PURE_STATIC_PROPS: &[(&str, &str)] = &[
    ("Math", "PI"),
    ("Math", "E"),
    ("Math", "LN2"),
    ("Math", "LN10"),
    ("Math", "LOG2E"),
    ("Math", "LOG10E"),
    ("Math", "SQRT2"),
    ("Math", "SQRT1_2"),
    ("Number", "EPSILON"),
    ("Number", "MAX_SAFE_INTEGER"),
    ("Number", "MIN_SAFE_INTEGER"),
    ("Number", "MAX_VALUE"),
    ("Number", "MIN_VALUE"),
    ("Number", "POSITIVE_INFINITY"),
    ("Number", "NEGATIVE_INFINITY"),
    ("Number", "NaN"),
    ("Symbol", "iterator"),
    ("Symbol", "asyncIterator"),
    ("Symbol", "toStringTag"),
    ("Symbol", "toPrimitive"),
    ("Symbol", "hasInstance"),
    ("Symbol", "species"),
    ("Symbol", "isConcatSpreadable"),
    ("Symbol", "match"),
    ("Symbol", "replace"),
    ("Symbol", "search"),
    ("Symbol", "split"),
];

/// Static methods that are Pure regardless of argument values.
/// Everything in this table must satisfy: per ECMA-262, the call
/// fires no user-defined code on any argument type — no `ToNumber`
/// / `ToString` / `ToPrimitive` / `ToPropertyKey` coercion, no
/// iterator protocol, no proxy trap, no own-property `[[Get]]`,
/// no mutation of any reachable object. See docs/design.md A8 for the
/// admission contract; AGENTS.md "Pure-call whitelist soundness"
/// for the agent-facing rule. New entries land only with a spec
/// citation showing no user-callback path; "common in practice"
/// is not sufficient.
pub(crate) const PURE_STATIC_CALLS: &[(&str, &str)] = &[
    // Type predicate — checks the IsArray internal slot. Spec
    // explicitly says: "does not perform a call to ToObject on its
    // argument".
    ("Array", "isArray"),
    // Number predicates — `Type(arg) is not Number ⇒ false`,
    // otherwise inspect the value. No coercion path.
    ("Number", "isFinite"),
    ("Number", "isInteger"),
    ("Number", "isNaN"),
    ("Number", "isSafeInteger"),
    // `Object.is(value1, value2)` — ECMA-262 §20.1.2.13 returns
    // `SameValue(value1, value2)`. SameValue (§7.2.11) dispatches on
    // the argument Types and compares structurally; it performs no
    // `ToNumber` / `ToString` / `ToPrimitive` coercion, fires no
    // iterator/proxy/getter path, and mutates nothing. Pure on any
    // argument values.
    ("Object", "is"),
];

/// Pure global callables (no receiver). Same admission contract as
/// `PURE_STATIC_CALLS`: the call must fire no user code on any
/// argument value.
pub(crate) const PURE_GLOBAL_CALLS: &[&str] = &[
    // ToBoolean is type-cased and fires no callbacks (objects are
    // unconditionally `true`; primitives are checked structurally).
    "Boolean",
];

/// Pure global callables when every argument is a primitive
/// literal (`Lit::Str` / `Lit::Num` / `Lit::Bool` / `Lit::Null` /
/// `Lit::BigInt`). The non-literal-arg form falls through to
/// `Unknown` because the spec-defined coercion path (`ToString`,
/// `ToNumber`, …) on a non-primitive value can fire user-defined
/// `[Symbol.toPrimitive]` / `valueOf` / `toString` and
/// observably modify state.
///
/// Soundness contract per entry:
/// * `Symbol`: ECMA-262 §20.4.1.1 — `Symbol(description)` does
///   `ToString(description)` (or skips it if description is undefined)
///   and returns a fresh symbol. `ToString` on a primitive literal
///   runs no user code, so the call has no observable side effect
///   beyond the fresh symbol. `Symbol` without `new`; `new Symbol(...)`
///   throws TypeError, but `new`-call form is `Expr::New` not
///   `Expr::Call`, so this rule never fires for it.
pub(crate) const PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS: &[&str] = &["Symbol"];

/// Built-in container constructors whose `new X()` (no args)
/// form is pure. ECMA-262 spec for each construct algorithm:
/// step 1 short-circuits when iterable/length is undefined,
/// returning a fresh empty container without invoking any user
/// code (no iterator protocol, no getters fired). Same admission
/// contract as `PURE_GLOBAL_CALLS`. `Set` / `Map` also accept
/// an Array-literal iterable; see `PURE_BUILTIN_NEW_ARRAY_ITERABLE`.
///
/// WHATWG platform constructors, same no-arg contract:
/// * `TextDecoder`: Encoding §"new TextDecoder(label, options)" —
///   label defaults to `"utf-8"`, which is a known label, so the
///   no-arg form cannot reach the RangeError throw path and runs no
///   user code. The WITH-label form is intentionally NOT admitted:
///   an invalid label throws RangeError at construction, an
///   observable init effect under reordering, and validating labels
///   statically would mean embedding the encodings registry.
/// * `TextEncoder`: Encoding §"new TextEncoder()" — takes no
///   parameters at all; constructs a utf-8 encoder, no throw path,
///   no user code.
/// * `URLSearchParams`: URL §"new URLSearchParams(init)" — with init
///   absent the query list is empty; no parsing, no user code. The
///   one-string-arg form is also pure — see
///   `PURE_BUILTIN_NEW_STRING_LITERAL_ARG`.
pub(crate) const PURE_BUILTIN_NEW_NO_ARGS: &[&str] = &[
    "Map",
    "Set",
    "WeakMap",
    "WeakSet",
    "Array",
    "TextDecoder",
    "TextEncoder",
    "URLSearchParams",
];

/// Built-in constructors whose 1-arg form is pure when the argument
/// is a single string **literal** (`Lit::Str`, no spread). Admission
/// argument per entry:
/// * `URLSearchParams`: URL §"new URLSearchParams(init)", string
///   branch — the input is parsed as `application/x-www-form-urlencoded`,
///   which is defined total over arbitrary strings (never throws) and
///   fires no user code. A non-literal string-valued expression is NOT
///   admitted because proving string-ness statically would need value
///   tracking; a literal needs none.
///
/// `RegExp` is deliberately absent: even with literal pattern/flags
/// args, ECMA-262 §22.2.4 compiles the pattern at construction and
/// throws SyntaxError on an invalid one — a deterministic but
/// observable init effect under statement reordering. Admitting it
/// soundly requires statically validating the pattern against the
/// ECMA-262 grammar (a `regress`-style validator), tracked as the
/// ignore-reason of `inferred_pure_collection_constructors_with_literal_args_emit_no_s_cycle`.
pub(crate) const PURE_BUILTIN_NEW_STRING_LITERAL_ARG: &[&str] = &["URLSearchParams"];

/// Built-in container constructors whose 1-arg form is pure when
/// the argument is an Array literal with all-Pure elements (no holes;
/// spreads only when the source is itself a fresh Array literal or a
/// pure conditional between fresh Array literals):
///
/// * `Set`: `new Set([elt, ...])` — ECMA-262 §24.2.1.1 iterates
///   the iterable via the built-in Array iterator (no user code
///   on a fresh array literal) and calls `Set.prototype.add` per
///   element. SameValueZero on primitive keys fires no user
///   code; on object keys it's reference equality. Fresh array
///   of Pure elements ⇒ pure.
/// * `Map`: `new Map([[k, v], ...])` — ECMA-262 §24.1.1.1 same
///   iterator path on the outer Array, then `Get(entry, "0")` /
///   `Get(entry, "1")` (own data properties on a fresh entry
///   array, no getter), then `Map.prototype.set`. Pure when
///   every entry is itself a 2-element Array literal with Pure
///   key + value. Fresh-array spreads are flattened under the same
///   entry rule.
/// * `WeakSet` / `WeakMap`: NOT covered — they additionally
///   require object keys; primitives throw. Allowing them would
///   require verifying every element/key has object value class,
///   which the classifier doesn't track.
///
/// Stricter than just "Pure arg" because:
///   - `new Set(somePureFn())` could produce a non-iterable at
///     runtime (TypeError at `[Symbol.iterator]()`), which is
///     observable from the caller's standpoint.
///   - `new Set(spreadable)` invokes the iterable's
///     `[Symbol.iterator]()`, which can fire user code on
///     anything other than a literal array.
pub(crate) const PURE_BUILTIN_NEW_ARRAY_ITERABLE: &[&str] = &["Map", "Set"];

/// Static-property READS on these globals are Pure: the property
/// is an own data property of the receiver per ECMA-262 (no getter
/// fires) and accessing it has no observable side effect.
///
/// **Function-valued.** The resolved value is a callable. CALLING
/// it is NOT pure unless the same `(receiver, name)` pair also
/// appears in `PURE_STATIC_CALLS`. Every entry here MUST have both
/// a positive `static_function_ref_*_alias_is_pure` test AND a
/// negative `static_function_ref_*_call_remains_unknown` test
/// pinning that distinction. See AGENTS.md "Pure-call whitelist
/// soundness".
pub(crate) const PURE_STATIC_FUNCTION_REFS: &[(&str, &str)] = &[
    // All entries below are own data properties of the `Object`
    // built-in per ECMA-262 §20.1.2 — reads fire no getter. The
    // CALL of each is unsafe in distinct ways and intentionally
    // NOT in `PURE_STATIC_CALLS`:
    //   - `Object.defineProperty(t, k, d)` mutates `t`.
    //   - `Object.freeze(o)` mutates `o`'s descriptor table.
    //   - `Object.values(o)` / `Object.keys(o)` invoke
    //     `[[OwnPropertyKeys]]` and (for values) `[[Get]]` per
    //     key — fires user getters and Proxy traps.
    // The bare alias form `const define = Object.defineProperty;`
    // appears in real specs as a renamed shortcut.
    ("Object", "defineProperty"),
    ("Object", "defineProperties"),
    ("Object", "freeze"),
    ("Object", "values"),
    ("Object", "keys"),
    ("Object", "entries"),
    ("Object", "fromEntries"),
    ("Object", "getOwnPropertyDescriptor"),
    ("Object", "getOwnPropertyDescriptors"),
    ("Object", "getOwnPropertyNames"),
    ("Object", "getOwnPropertySymbols"),
    ("Object", "getPrototypeOf"),
    ("Object", "setPrototypeOf"),
    ("Object", "create"),
    ("Object", "assign"),
    ("Object", "is"),
    ("Object", "isFrozen"),
    ("Object", "isSealed"),
    ("Object", "isExtensible"),
    ("Object", "preventExtensions"),
    ("Object", "seal"),
    ("Object", "hasOwn"),
];

/// Static `Object` methods that are Pure when called with a single
/// argument that is structurally a fresh plain-data object/array
/// literal OR a chunk-top `ChunkBinding::PlainData` binding. Stricter
/// than `PURE_STATIC_CALLS` because each entry either invokes
/// `[[Get]]` on own keys (which fires user accessors on a general
/// receiver) or mutates descriptor state (`freeze`); the plain-data
/// receiver gate closes both holes. Per-entry ECMA-262 soundness +
/// the excluded shortlist (`Object.assign`,
/// `Object.getOwnPropertyNames`, `Array.from`) live in
/// <docs/purity_soundness.md> § "PURE_OBJECT_CALLS_ON_PLAIN_DATA".
pub(crate) const PURE_OBJECT_CALLS_ON_PLAIN_DATA: &[(&str, &str)] = &[
    ("Object", "keys"),
    ("Object", "values"),
    ("Object", "entries"),
    ("Object", "freeze"),
    ("Object", "fromEntries"),
];

/// Receiver / global-callable names whose whitelist firing depends
/// on the chunk not having shadowed them at top level.
/// `analyze_chunk` populates the shadowed-globals set, and
/// the classifier suppresses whitelist hits for any name in it —
/// e.g. `const Math = …` makes `Math.PI` fall back to `Unknown`.
pub(crate) const WHITELIST_RECEIVERS: &[&str] =
    &["Math", "Array", "Symbol", "Number", "Boolean", "Object"];

/// Every global name any whitelist table keys on, derived as the
/// union of all tables. `compute_shadowed_globals` tracks exactly
/// this set, so adding a table (or an entry naming a new global)
/// automatically extends shadow tracking — a name missing from the
/// tracked set would make the classifier blind to `const Map = …`
/// at chunk top and let `new Map()` classify `Pure` against a
/// user-defined value.
pub(crate) static SHADOW_TRACKED_GLOBALS: LazyLock<BTreeSet<&'static str>> = LazyLock::new(|| {
    let mut names: BTreeSet<&'static str> = BTreeSet::new();
    names.extend(WHITELIST_RECEIVERS.iter().copied());
    names.extend(PURE_STATIC_PROPS.iter().map(|(r, _)| *r));
    names.extend(PURE_STATIC_CALLS.iter().map(|(r, _)| *r));
    names.extend(PURE_STATIC_FUNCTION_REFS.iter().map(|(r, _)| *r));
    names.extend(PURE_OBJECT_CALLS_ON_PLAIN_DATA.iter().map(|(r, _)| *r));
    names.extend(PURE_GLOBAL_CALLS.iter().copied());
    names.extend(PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS.iter().copied());
    names.extend(PURE_BUILTIN_NEW_NO_ARGS.iter().copied());
    names.extend(PURE_BUILTIN_NEW_ARRAY_ITERABLE.iter().copied());
    names.extend(PURE_BUILTIN_NEW_STRING_LITERAL_ARG.iter().copied());
    names
});
