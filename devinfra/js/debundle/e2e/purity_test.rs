//! Pure-function recognition + spec-level purity declarations
//! for the realizability gate's `S` (side-effect ordering)
//! sub-graph.
//!
//! Most tests run in CI. A few remain `#[ignore]`d as desiderata
//! for future work — each ignore-reason names the specific
//! analysis still missing (fresh-literal-arg gate for
//! `Object.freeze`, `Expr::New` whitelist for `Set`/`Map`/`RegExp`,
//! escape analysis for IIFE patterns, lazy-back-edge I-cycle
//! relaxation for cross-module mutual recursion). Their bodies
//! become regression fixtures the moment those analyses land.
//!
//! # Background
//!
//! `classify_expr_purity` (in `purity.rs`) decides
//! per-expression whether a top-level statement contributes a
//! `SideEffect` row to the dep graph. It's currently very
//! conservative: any `Call`, `New`, `MemberExpression`,
//! `OptChain`, or `SuperProp` is `Purity::Unknown` (treated as
//! impure). This is sound but over-rejects realistic specs in
//! which most top-level calls are HOF wrappers, schema
//! builders, collection constructors, or static-method
//! aliases that have no observable side effects.
//!
//! Two complementary mechanisms close the gap:
//!
//! 1. **Inferred purity.** The walker recursively inspects
//!    function bodies in the chunk and marks calls/news pure
//!    when the callee is statically provably free of
//!    observable side effects. Built-ins (`Object.freeze`,
//!    `Set`/`Map` constructors with literal args, `Object`
//!    static methods, etc.) are pure by catalogue;
//!    user-defined functions are pure by recursive analysis
//!    (no `globalThis`/`window`/`self` mutation, no
//!    `console.*`, no `delete`, no calls to known-impure
//!    functions, no member-expression read of unknown-shape
//!    objects, etc.).
//!
//! 2. **Declared purity.** The spec author annotates a
//!    logical-module member with `purity: "pure"` to assert
//!    that calls to the bound function have no observable
//!    side effects. The validator does not second-guess the
//!    annotation: it is an author trust contract. Used when
//!    the function body is too dynamic, too large, or
//!    imported from outside the chunk for static analysis to
//!    handle.
//!
//! # Out of scope
//!
//! Both mechanisms only suppress `S` (side-effect ordering)
//! edges. They do **not** affect `R` (at-init read) edges:
//! a top-level statement that reads a binding owned by
//! another logical module still contributes an `R` edge
//! regardless of how the surrounding call is classified.
//! Realizability gating against TDZ-causing cycles is
//! orthogonal to purity inference.
//!
//! # Spec annotation shape
//!
//! ```yaml
//! {
//!   "name": "defineComponent",
//!   "selector": { "binding": { "name": "B" } },
//!   "purity": "pure"   // ← new optional field
//! }
//! ```
//!
//! Only `"pure"` is defined here. Future extensions could
//! include `"commutative"` (the function has side effects but
//! they're observably commutative across calls — useful for
//! registry-add patterns where last-write-wins or set-union
//! semantics make ordering irrelevant).
//!
//! The annotation lives at the member level, not on the
//! selector, because it's metadata about the binding, not
//! about how to find it.

use debundle_e2e_support::*;
use serde_json::json;

// ---------------------------------------------------------------------------
// Inferred purity — `classify_expr_purity` recognizes shapes statically.
// ---------------------------------------------------------------------------

#[test]
fn inferred_pure_hof_wrapper_across_modules_emits_no_s_cycle() {
    // The HOF `wrap` takes a function and returns a struct
    // containing it. Body has no observable side effects: no
    // global writes, no calls into unknown territory. The
    // analyzer should classify `wrap(...)` calls as pure.
    //
    // The chunk has three `wrap(...)` top-level statements
    // interleaved across two modules. Today the conservative
    // classifier flags each as side-effecting and the resulting
    // `S` graph has cross-module edges in both directions —
    // an `S` cycle that the gate rejects. With inferred purity,
    // no `S` edges are emitted between the modules and the
    // build accepts.
    let fixture = run_fixture(FixtureOpts::new(
        r#"function wrap(f) { return { kind: "wrapped", impl: f }; }
const A = wrap(function () { return "a"; });
const B = wrap(function () { return "b"; });
const C = wrap(function () { return "c"; });
console.log(A.impl(), B.impl(), C.impl());
export { A, B, C };
"#,
        vec![
            logical_module("mod_a", &[Member::new("A"), Member::new("C")]),
            logical_module("mod_b", &[Member::new("B")]),
        ],
    ));
    assert_entry_output(&fixture, "a b c\n");
}

#[test]
#[ignore = "blocked on Object.freeze whitelist with fresh-literal-arg gate \
            (Step D in the purity-desiderata follow-up plan)"]
fn inferred_pure_schema_builder_across_modules_emits_no_s_cycle() {
    // `Object.freeze` is a statically-known pure built-in;
    // `schema(spec)` returns the frozen spec object. All `schema(...)`
    // top-level statements in the chunk should be classified pure.
    let fixture = run_fixture(FixtureOpts::new(
        r#"function schema(spec) { return Object.freeze(spec); }
const userSchema = schema({ kind: "user" });
const productSchema = schema({ kind: "product" });
const orderSchema = schema({ kind: "order" });
console.log(userSchema.kind, productSchema.kind, orderSchema.kind);
export { userSchema, productSchema, orderSchema };
"#,
        vec![
            logical_module(
                "mod_a",
                &[Member::new("userSchema"), Member::new("orderSchema")],
            ),
            logical_module("mod_b", &[Member::new("productSchema")]),
        ],
    ));
    assert_entry_output(&fixture, "user product order\n");
}

#[test]
#[ignore = "blocked on Expr::New whitelist with per-constructor arg-shape gate \
            (Step E in the purity-desiderata follow-up plan)"]
fn inferred_pure_collection_constructors_with_literal_args_emit_no_s_cycle() {
    // `new Set([...])`, `new Map([...])`, `new RegExp("...")` with
    // literal-only args are pure by built-in catalogue. The
    // current classifier marks all `Expr::New` as `Unknown`.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const tagsA = new Set(["alpha", "beta"]);
const tagsB = new Set(["gamma"]);
const cfgA = new Map([["k1", 1]]);
const cfgB = new Map([["k2", 2]]);
const reA = new RegExp("a+");
const reB = new RegExp("b+");
console.log(tagsA.size, tagsB.size, cfgA.size, cfgB.size, reA.test("aa"), reB.test("bbb"));
export { tagsA, tagsB, cfgA, cfgB, reA, reB };
"#,
        vec![
            logical_module(
                "mod_a",
                &[
                    Member::new("tagsA"),
                    Member::new("cfgA"),
                    Member::new("reA"),
                ],
            ),
            logical_module(
                "mod_b",
                &[
                    Member::new("tagsB"),
                    Member::new("cfgB"),
                    Member::new("reB"),
                ],
            ),
        ],
    ));
    assert_entry_output(&fixture, "2 1 1 1 true true\n");
}

#[test]
fn inferred_pure_static_object_method_aliasing_emits_no_s_cycle() {
    // The expression being classified here is bare member
    // access — `const define = Object.defineProperty;` reads
    // a property off the `Object` global without invoking it.
    // (Calling `Object.defineProperty(...)` mutates its
    // target; that's not what's classified pure here.) Today
    // the classifier marks any `MemberExpression` as
    // `Unknown` because any object can have a getter; for the
    // well-known `Object` global, the analyzer should
    // recognize that no standard property is a getter, so
    // reading the function reference is side-effect-free.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const define = Object.defineProperty;
const freeze = Object.freeze;
const values = Object.values;
const keys = Object.keys;
const obj = freeze({ x: 1 });
console.log(values(obj)[0], keys(obj)[0]);
export { define, freeze, values, keys };
"#,
        vec![
            logical_module("mod_a", &[Member::new("define"), Member::new("values")]),
            logical_module("mod_b", &[Member::new("freeze"), Member::new("keys")]),
        ],
    ));
    // Log primitives directly: Node's `util.inspect` array
    // formatting (`[ 1 ]` vs `[1]`) shifts across versions; a
    // primitive comparison is version-stable.
    assert_entry_output(&fixture, "1 x\n");
}

#[test]
#[ignore = "blocked on local-mutation / escape analysis (track that an assignment \
            mutates only a fresh parameter that doesn't escape — Step F in the \
            purity-desiderata follow-up plan; deferred until a real chunk needs it)"]
fn inferred_pure_iife_enum_builder_emits_no_s_cycle() {
    // The minified enum-builder pattern: an IIFE whose body
    // mutates its own parameter (a fresh object) and returns
    // it. All writes are local to the parameter; no globals
    // are touched. The whole expression is pure.
    let fixture = run_fixture(FixtureOpts::new(
        r#"var Status = ((n) => ((n.OK = 0), (n.WARN = 1), (n.ERROR = 2), n))({});
var Color = ((n) => ((n.RED = "r"), (n.GREEN = "g"), (n.BLUE = "b"), n))({});
var Tag = ((n) => ((n[(n.SMALL = 0)] = "small"), (n[(n.LARGE = 1)] = "large"), n))({});
console.log(Status.OK, Color.RED, Tag.SMALL, Tag[1]);
export { Status, Color, Tag };
"#,
        vec![
            logical_module("mod_a", &[Member::new("Status"), Member::new("Tag")]),
            logical_module("mod_b", &[Member::new("Color")]),
        ],
    ));
    assert_entry_output(&fixture, "0 r 0 large\n");
}

#[test]
fn inferred_pure_recursive_function_classified_pure() {
    // Mutually recursive pure functions: `even(n)` calls
    // `odd(n - 1)`; `odd(n)` calls `even(n - 1)`. Recursive
    // analysis must terminate (memoize per-function visit
    // status) and conclude both pure.
    let fixture = run_fixture(FixtureOpts::new(
        r#"function even(n) { return n === 0 ? true : odd(n - 1); }
function odd(n) { return n === 0 ? false : even(n - 1); }
const fourEven = even(4);
const fiveOdd = odd(5);
console.log(fourEven, fiveOdd);
export { even, odd, fourEven, fiveOdd };
"#,
        vec![
            logical_module("mod_a", &[Member::new("even"), Member::new("fourEven")]),
            logical_module("mod_b", &[Member::new("odd"), Member::new("fiveOdd")]),
        ],
    ));
    assert_entry_output(&fixture, "true true\n");
}

// ---------------------------------------------------------------------------
// Inferred purity — negative cases. Real side effects must still register.
// ---------------------------------------------------------------------------

#[test]
fn inferred_impure_globalthis_write_still_rejected() {
    // Even with a smarter classifier, `globalThis.X = ...` is
    // unambiguously observably impure: the assignment is
    // visible to anything that reads `globalThis.X` later.
    // Interleaved `globalThis.tag = "..."` writes across two
    // modules close an `S` cycle that the gate must still
    // reject — the relaxation in this PR's main purity-
    // inference work must not over-relax to allow this.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const a1 = (globalThis.tag = "a1", 1);
const b1 = (globalThis.tag = "b1", 2);
const a2 = (globalThis.tag = "a2", 3);
console.log(a1, a2, b1, globalThis.tag);
export { a1, a2, b1 };
"#,
            vec![
                logical_module("mod_a", &[Member::new("a1"), Member::new("a2")]),
                logical_module("mod_b", &[Member::new("b1")]),
            ],
        ),
        &["cycle", "mod_a", "mod_b", "side-effect"],
    );
}

#[test]
fn inferred_impure_console_log_still_rejected() {
    // `console.log` writes to the host's stdout — observable
    // I/O. A function that calls `console.log` must be
    // classified impure. Top-level statements that invoke
    // such a function across modules must still produce S
    // edges and reject the spec.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"function logged(x) { console.log("init:", x); return x; }
const a1 = logged("a1");
const b1 = logged("b1");
const a2 = logged("a2");
console.log(a1, b1, a2);
export { a1, a2, b1 };
"#,
            vec![
                logical_module("mod_a", &[Member::new("a1"), Member::new("a2")]),
                logical_module("mod_b", &[Member::new("b1")]),
            ],
        ),
        &["cycle", "mod_a", "mod_b", "side-effect"],
    );
}

#[test]
fn inferred_impure_via_transitively_impure_callee_still_rejected() {
    // `caller(...)` does nothing observable on its own, but
    // calls `tainted()` which mutates `globalThis`. Recursive
    // purity analysis must propagate impurity from `tainted`
    // back to `caller` and reject specs that distribute
    // `caller(...)` calls across modules in interleaved
    // source order.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"function tainted() { globalThis.touched = true; return 1; }
function caller(label) { tainted(); return { label }; }
const a1 = caller("a1");
const b1 = caller("b1");
const a2 = caller("a2");
console.log(a1.label, b1.label, a2.label, globalThis.touched);
export { a1, a2, b1 };
"#,
            vec![
                logical_module("mod_a", &[Member::new("a1"), Member::new("a2")]),
                logical_module("mod_b", &[Member::new("b1")]),
            ],
        ),
        &["cycle", "mod_a", "mod_b", "side-effect"],
    );
}

// ---------------------------------------------------------------------------
// Declared purity — author asserts purity via `member.purity = "pure"`.
// ---------------------------------------------------------------------------

#[test]
fn declared_pure_member_suppresses_s_edges_for_opaque_call() {
    // `dispatcher` does dynamic property access into a
    // registry — recursive purity analysis can't statically
    // prove the lookup is pure (the registry value at the
    // dynamic key could be a getter or a function that
    // mutates state). The spec author knows the registry
    // entries are all pure handlers, so they annotate the
    // `dispatcher` member with `purity: "pure"`. The
    // validator trusts the assertion and stops emitting
    // `S` edges for `dispatcher(...)` call sites.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const registry = {
  alpha: function (x) { return { handled: "alpha", x }; },
  beta: function (x) { return { handled: "beta", x }; },
  gamma: function (x) { return { handled: "gamma", x }; },
};
function dispatcher(req) {
  return registry[req.kind](req.payload);
}
const A = dispatcher({ kind: "alpha", payload: 1 });
const B = dispatcher({ kind: "beta", payload: 2 });
const C = dispatcher({ kind: "gamma", payload: 3 });
console.log(A.handled, B.handled, C.handled);
export { A, B, C, dispatcher };
"#,
        vec![
            (
                "mod_a".to_string(),
                json!({
                    "members": [
                        { "name": "A", "selector": { "binding": { "name": "A" } } },
                        { "name": "C", "selector": { "binding": { "name": "C" } } },
                        {
                            "name": "dispatcher",
                            "selector": { "binding": { "name": "dispatcher" } },
                            // ↓ author asserts: calls to `dispatcher`
                            //   have no observable side effects. Both
                            //   call sites in mod_a (A, C) and the
                            //   one in mod_b (B) drop their `S` edges.
                            "purity": "pure",
                        },
                    ],
                }),
            ),
            (
                "mod_b".to_string(),
                json!({
                    "members": [
                        { "name": "B", "selector": { "binding": { "name": "B" } } },
                    ],
                }),
            ),
        ],
    ));
    assert_entry_output(&fixture, "alpha beta gamma\n");
}

#[test]
fn declared_pure_member_does_not_bypass_at_init_read_cycle() {
    // Declared purity affects only `S` (side-effect ordering)
    // edges. It does *not* suppress `R` (at-init read) edges.
    // Here `wrap(...)` is annotated pure, but `wrap(B)` in
    // mod_a still reads `B` from mod_b at-init, and `wrap(A)`
    // in mod_b still reads `A` from mod_a at-init: the `R`
    // graph has a cycle and the gate must still reject.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"function wrap(x) { return { ref: x }; }
const A = "a";
const B = wrap(A);
const D = wrap(B);
console.log(B.ref, D.ref);
export { A, B, D, wrap };
"#,
            vec![
                (
                    "mod_a".to_string(),
                    json!({
                        "members": [
                            { "name": "A", "selector": { "binding": { "name": "A" } } },
                            { "name": "D", "selector": { "binding": { "name": "D" } } },
                            {
                                "name": "wrap",
                                "selector": { "binding": { "name": "wrap" } },
                                // Author asserts purity — `S` edges
                                // through `wrap(...)` calls go away.
                                "purity": "pure",
                            },
                        ],
                    }),
                ),
                (
                    "mod_b".to_string(),
                    json!({
                        "members": [
                            { "name": "B", "selector": { "binding": { "name": "B" } } },
                        ],
                    }),
                ),
            ],
        ),
        // R-edge mod_a → mod_b (D's init reads B); R-edge
        // mod_b → mod_a (B's init reads A). The cycle's
        // evidence is at-init reads, not the wrapped call.
        // The rejection message must still mention the cycle
        // and both modules.
        &["cycle", "mod_a", "mod_b"],
    );
}

#[test]
fn declared_pure_annotation_applies_only_to_annotated_member_positive() {
    // Two HOFs in the chunk: `pureWrap` (has no observable
    // side effects) and `impureWrap` (legitimately writes to
    // `globalThis`). The author annotates only `pureWrap` as
    // pure; `impureWrap` is left at the default
    // (conservatively impure) classification.
    //
    // Positive half of the contract: a spec that interleaves
    // `pureWrap(...)` calls across modules accepts — the
    // annotation drops the `S` edges that would otherwise
    // close a cycle. The companion `..._negative` test pins
    // the other half: the annotation does not bleed onto
    // unannotated members.
    let fixture = run_fixture(FixtureOpts::new(
        r#"function pureWrap(x) { return { val: x }; }
function impureWrap(x) { globalThis.lastWrap = x; return { val: x }; }
const A = pureWrap("a");
const B = pureWrap("b");
const C = pureWrap("c");
console.log(A.val, B.val, C.val);
export { A, B, C, pureWrap, impureWrap };
"#,
        vec![
            (
                "mod_a".to_string(),
                json!({
                    "members": [
                        { "name": "A", "selector": { "binding": { "name": "A" } } },
                        { "name": "C", "selector": { "binding": { "name": "C" } } },
                        {
                            "name": "pureWrap",
                            "selector": { "binding": { "name": "pureWrap" } },
                            "purity": "pure",
                        },
                        {
                            "name": "impureWrap",
                            "selector": { "binding": { "name": "impureWrap" } },
                            // No `purity` annotation — default
                            // (impure) classification applies.
                        },
                    ],
                }),
            ),
            (
                "mod_b".to_string(),
                json!({
                    "members": [
                        { "name": "B", "selector": { "binding": { "name": "B" } } },
                    ],
                }),
            ),
        ],
    ));
    assert_entry_output(&fixture, "a b c\n");
}

#[test]
fn declared_pure_annotation_applies_only_to_annotated_member_negative() {
    // Same chunk shape as the `..._positive` companion (two
    // HOFs, `pureWrap` annotated pure, `impureWrap` not), but
    // the spec interleaves `impureWrap(...)` calls across
    // mod_a and mod_b. Because `impureWrap` carries no
    // annotation and its body writes `globalThis.lastWrap`,
    // each call is classified side-effecting; the resulting
    // `S` graph has cross-module edges in both directions and
    // the gate must reject. The `pureWrap` annotation does
    // not bleed onto sibling members.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"function pureWrap(x) { return { val: x }; }
function impureWrap(x) { globalThis.lastWrap = x; return { val: x }; }
const A = impureWrap("a");
const B = impureWrap("b");
const C = impureWrap("c");
console.log(A.val, B.val, C.val, globalThis.lastWrap);
export { A, B, C, pureWrap, impureWrap };
"#,
            vec![
                (
                    "mod_a".to_string(),
                    json!({
                        "members": [
                            { "name": "A", "selector": { "binding": { "name": "A" } } },
                            { "name": "C", "selector": { "binding": { "name": "C" } } },
                            {
                                "name": "pureWrap",
                                "selector": { "binding": { "name": "pureWrap" } },
                                "purity": "pure",
                            },
                            {
                                "name": "impureWrap",
                                "selector": { "binding": { "name": "impureWrap" } },
                                // Deliberately unannotated.
                            },
                        ],
                    }),
                ),
                (
                    "mod_b".to_string(),
                    json!({
                        "members": [
                            { "name": "B", "selector": { "binding": { "name": "B" } } },
                        ],
                    }),
                ),
            ],
        ),
        &["cycle", "mod_a", "mod_b", "side-effect"],
    );
}

// ---------------------------------------------------------------------------
// Builtin constructors — `new Container()` purity.
// ---------------------------------------------------------------------------

// `new Map()`/`Set()`/`WeakMap()`/`WeakSet()`/`Array()` allocate
// the fresh container and return — no iterator protocol, no
// user-code getters. Same admission contract as
// `PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS` / `PURE_GLOBAL_CALLS`.
// Also covers the array-literal-iterable form for `Set`/`Map`:
// built-in Array iterator + primitive-key Set.add/Map.set fire
// no user code, so an Array-literal arg with all-Pure
// elements (no spreads) is also pure.
//
// Cycle-forcing fixture: `b = new <Container>();` peeled to
// b_module while previous-SE neighbor `a` (IIFE call) and a
// downstream reader of `b` stay in residual. Without the rule
// `new …` is Unknown → b is SE → b → a s-edge crosses into
// residual; combined with residual → b_module (c reads b at
// init), spec is unrealizable. With the rule `new <Container>()`
// is Pure → b drops out of the S-chain.

#[test]
fn new_map() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Map();
const c = b.size + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Map()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn new_set() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Set();
const c = b.size + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Set()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn new_weakmap() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new WeakMap();
const c = (b ? "y" : "n") + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new WeakMap()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "y1\n");
}

#[test]
fn new_array() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Array();
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Array()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn new_set_with_array_of_primitives() {
    // `new Set([prim_lit, prim_lit, ...])` — Array literal with
    // all-Pure elements. ECMA-262 §24.2.1.1: iterates via the
    // built-in Array iterator and calls `Set.add` per element;
    // no user code on primitive keys.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Set(["x", "y", "z"]);
const c = b.size + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Set("],
        &["const a"],
    );
    assert_entry_output(&fixture, "4\n");
}

#[test]
fn new_map_with_array_of_pure_pairs() {
    // `new Map([[k, v], [k, v], ...])` — Array of 2-tuple Array
    // literals with primitive keys + pure values. Map's
    // construct path Get's [0]/[1] of each entry (own data
    // properties on a fresh Array, no getter) and Map.set's
    // them (primitive key SameValueZero, no user code).
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Map([["x", 1], ["y", 2]]);
const c = b.get("x") + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Map(", r#""x""#],
        &["const a"],
    );
    assert_entry_output(&fixture, "2\n");
}

#[test]
fn class_static_new_map_field() {
    // Class-declaration shape: `static x = new Map()` is the
    // only static-side-effect candidate. Without the rule the
    // class is flagged side-effecting and pulled into the
    // S-chain.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
class C {
  static x = new Map();
}
const c = C.x.size + a;
console.log(c);
export { a, C, c };
"#,
        vec![logical_module("c_module", &[Member::new("C")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/c_module.js",
        &["class C", "static x = new Map()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "1\n");
}

// ---------------------------------------------------------------------------
// chunk_renames purity propagation.
// ---------------------------------------------------------------------------

// Pin `purity: pure` propagation from chunk_renames members
// into `declared_pure`.
//
// `Member.purity` is collected only from logical-module
// members today. `chunk_renames.members[].purity` and
// `residual_modules.members[].purity` are silently dropped.
// That forces the spec author to either peel the binding into
// a 1-member logical module just to get the purity hint
// propagated, or leave the call classified `Unknown`.
//
// Refinement: chunk_renames + residual_modules members with
// `purity: pure` contribute to `declared_pure` alongside
// logical-module members. One spec entry per imported function
// carries both the rename (via the existing chunk_renames
// pipeline) and the purity hint.
//
// In-residual rename behavior is already pinned by
// `chunk_renames_test`; this test focuses on the purity-side
// propagation.
#[test]
fn chunk_rename_with_purity_pure_propagates_to_call_classifier() {
    // Fixture:
    //   - vendor.js exports a function `f`.
    //   - entry imports `f as cx`.
    //   - `const a = (() => 1)();`  — SE, stays in residual
    //   - `const b = cx();`          — would be SE without the rule
    //                                  (imported, not in declared_pure)
    //   - `const c = a + b;`         — reads b at init
    //   - peel target: b → b_module
    //
    // Without the rule:
    //   cx() is Unknown → b is SE → b → a s-edge across
    //   b_module → residual. Combined with residual → b_module
    //   (c's at-init read of b), cycle.
    //
    // With the rule:
    //   chunk_renames carries `purity: pure` for cx → cx in
    //   declared_pure → cx() is Pure → b is Pure → no S-chain
    //   participation. Only edge: residual → b_module. DAG.
    let opts = FixtureOpts {
        source: r#"import { f as cx } from "./vendor.js";
const a = (() => 1)();
const b = cx();
const c = a + b;
console.log(c);
export { a, b, c };
"#,
        logical_modules: vec![logical_module("b_module", &[Member::new("b")])],
        residual: None,
        chunk_renames: Some(json!({
            "id": "chunk_renames__static_app",
            "members": [
                {
                    "name": "getMobxGlobalState",
                    "selector": {
                        "binding": {
                            "name": "cx",
                            "kind": "import_specifier",
                        },
                    },
                    "purity": "pure",
                },
            ],
        })),
        chunk_id: "static/app",
        include_residual: true,
        unassigned_mode: None,
        extra_files: &[(
            "static/app/vendor.js",
            "export function f() { return 1; }\n",
        )],
    };
    let fixture = run_fixture(opts);

    // The peel succeeded: b is in b_module without dragging a.
    // The fact that the build didn't error on a cycle proves
    // that `cx()` was classified Pure by the call classifier.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = "],
        &["const a", "(()=>1)"],
    );

    // Behaviour preserved: c == a + b == 1 + 1 == 2.
    assert_entry_output(&fixture, "2\n");
}

// ---------------------------------------------------------------------------
// OptChain purity.
// ---------------------------------------------------------------------------

// Pin OptChain purity classification.
//
// `window?.foo?.bar` is observably equivalent to `window.foo.bar`
// modulo the short-circuit when `window` or `window.foo` is
// null/undefined. Optional chaining doesn't add semantic side
// effects of its own; it only short-circuits.
//
// Today `purity::classify_expr_purity` returns
// `Purity::Unknown` for any `Expr::OptChain` regardless of what
// it expands to, so a `var x = window?.X?.Y;` initializer is
// classified `has_side_effect = true` even when the underlying
// chain reads only safe globals. That spurious has_side_effect
// makes the var participate in the side-effect-order chain, and
// a peel proposal that would otherwise be a clean
// Direct-peelable singleton is forced to drag in whatever the
// immediately-prior side-effecting owner happens to be.
//
// On Tana this manifests as the `dg = window?.Meticulous?.…`
// declarator being chained to the constructor-call declarator
// `Lge = new $g()` that precedes it in the same comma-list,
// creating a cross-module side-effect-order edge that closes a
// 4-module cycle (apply_decorators → tana_logger →
// test_detection → workspace/invite/state) once the spec
// claims dg in its proper home.
//
// Refinement under test: when `Expr::OptChain` is encountered,
// recurse through its base (`OptChainBase::Member` /
// `OptChainBase::Call`) and classify by the underlying access.
// For static-property reads on a whitelisted receiver (Math,
// Array, …), this returns `Pure`. The Tana case
// (`window?.Meticulous?.…`) needs R2 (extending the
// whitelist to host globals) on top — this test pins R1
// using a receiver that's already on the whitelist.
#[test]
fn optional_chain_on_whitelisted_receiver_classified_pure() {
    // Cycle-forcing fixture:
    //   1. const X = (() => "x")();              — side-effecting (IIFE call); stays in residual
    //   2. const Y = Number?.MAX_SAFE_INTEGER;   — currently OptChain → Unknown → side-effecting; peeled to y_module
    //   3. const Z = Y + 1;                       — reads Y at init; stays in residual
    //   4. console.log(Z);
    //   5. export { X, Y, Z };
    //
    // Today (no R1):
    //   Y has has_side_effect=true (OptChain → Unknown).
    //   S-edges (transitive reduction over SE owners):
    //     Y → X        (Y depends on X via source order, both SE)
    //     console.log(Z) → Y
    //   After peeling Y to y_module, the schedule sees:
    //     y_module → residual (from Y → X s-edge)
    //     residual → y_module (residual reads Y at init via const Z = Y + 1)
    //   That's a cycle in I ∪ S. Validator rejects the spec.
    //
    // After R1:
    //   Y is `Pure` (OptChain recurses into Number.MAX_SAFE_INTEGER
    //   which is whitelisted), so Y has has_side_effect=false.
    //   No Y → X s-edge. Only edge: residual → y_module
    //   (Z's at-init read of Y). DAG. Validator accepts.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const X = (() => "x")();
const Y = Number?.MAX_SAFE_INTEGER;
const Z = Y + 1;
console.log(Z);
export { X, Y, Z };
"#,
        vec![logical_module("y_module", &[Member::new("Y")])],
    ));

    // y_module owns Y but does NOT own X — X stays in residual.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/y_module.js",
        &["const Y = Number?.MAX_SAFE_INTEGER", "export {", "Y"],
        &["const X", "(()=>\"x\")"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["const X", "const Z"],
        &["const Y"],
    );

    // Behaviour preserved: console.log(Z) prints
    // String(Number.MAX_SAFE_INTEGER + 1).
    assert_entry_output(&fixture, "9007199254740992\n");
}

// ---------------------------------------------------------------------------
// Symbol purity.
// ---------------------------------------------------------------------------

// Pin `Symbol(primitive_literal)` purity classification.
//
// ECMA-262 §20.4.1.1 says `Symbol(description)`:
//   1. If NewTarget is not undefined, throw TypeError.
//   2. If description is undefined, let descString be undefined.
//   3. Else, let descString be ? ToString(description).
//   4. Return a new unique Symbol value whose [[Description]]
//      is descString.
//
// For a primitive-literal `description` (string, number,
// boolean, null, bigint) the `ToString` step runs no user code,
// so the call has no observable side effects beyond producing a
// fresh symbol primitive. Same admission contract as the
// existing `PURE_GLOBAL_CALLS` whitelist (`Boolean`).
//
// Without this rule, the classifier returns `Purity::Unknown`
// for any `Symbol(...)` call, the declarator is flagged
// `has_side_effect = true`, and the binding gets pulled into the
// source-order side-effect-order chain. In real chunks that's
// enough to close phantom multi-module cycles when the spec
// tries to peel a `Symbol`-bound brand declarator into its own
// module.
#[test]
fn symbol_with_string_literal_arg_classified_pure() {
    // Cycle-forcing fixture: the spec peels `b` (a
    // `Symbol(string-literal)`) into `b_module`. Source-order
    // surrounds `b` with one preceding side-effecting declarator
    // `a` (the IIFE call) so b would inherit a previous-SE
    // s-edge to a if it were classified side-effecting, plus a
    // following declarator `c` whose initializer reads `b` at
    // init so residual has a forward read-dep into b_module.
    //
    // Without this rule: Symbol(...) is Unknown → b is SE →
    // b → a s-edge → cross-module b_module → residual after
    // peel. Combined with residual → b_module (c reads b at
    // init), spec is unrealizable.
    //
    // With this rule: Symbol("b") is Pure → b is not SE → no
    // b → a s-edge. Only edge: residual → b_module. DAG.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = Symbol("b");
const c = b.description + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = Symbol(", "export {", "b"],
        &["const a", "(()=>1)"],
    );
    assert_entry_output(&fixture, "b1\n");
}

#[test]
fn symbol_with_no_args_classified_pure() {
    // No description argument — pure under the same rule
    // (ECMA-262 §20.4.1.1 step 2: descString is undefined when
    // description is undefined, no ToString call).
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = Symbol();
const c = (typeof b) + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = Symbol()", "export {", "b"],
        &["const a"],
    );
    assert_entry_output(&fixture, "symbol1\n");
}
