//! **Desiderata.** Pure-function recognition + spec-level
//! purity declarations for the realizability gate's `S`
//! (side-effect ordering) sub-graph.
//!
//! Every test in this file is `#[ignore]`d: the features they
//! exercise aren't implemented yet. The file exists to define
//! the contract — when these features land, the `#[ignore]`
//! attributes get removed and the tests start running in CI.
//! Until then, they document what the validator should be able
//! to do, with concrete fixtures that double as acceptance
//! criteria.
//!
//! # Background
//!
//! `classify_expr_purity` (in `schedule_validator.rs`) decides
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
//! ```jsonc
//! {
//!   "id": "m_define_component",
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
#[ignore = "purity inference unimplemented"]
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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
#[ignore = "purity inference unimplemented"]
fn inferred_pure_schema_builder_across_modules_emits_no_s_cycle() {
    // `Object.freeze` is a statically-known pure built-in;
    // `schema(spec)` returns the frozen spec object. All `schema(...)`
    // top-level statements in the chunk should be classified pure.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
#[ignore = "purity inference unimplemented"]
fn inferred_pure_collection_constructors_with_literal_args_emit_no_s_cycle() {
    // `new Set([...])`, `new Map([...])`, `new RegExp("...")` with
    // literal-only args are pure by built-in catalogue. The
    // current classifier marks all `Expr::New` as `Unknown`.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
#[ignore = "purity inference unimplemented"]
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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
#[ignore = "purity inference unimplemented"]
fn inferred_pure_iife_enum_builder_emits_no_s_cycle() {
    // The minified enum-builder pattern: an IIFE whose body
    // mutates its own parameter (a fresh object) and returns
    // it. All writes are local to the parameter; no globals
    // are touched. The whole expression is pure.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
#[ignore = "purity inference unimplemented"]
fn inferred_pure_recursive_function_classified_pure() {
    // Mutually recursive pure functions: `even(n)` calls
    // `odd(n - 1)`; `odd(n)` calls `even(n - 1)`. Recursive
    // analysis must terminate (memoize per-function visit
    // status) and conclude both pure.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
#[ignore = "purity inference unimplemented"]
fn inferred_impure_globalthis_write_still_rejected() {
    // Even with a smarter classifier, `globalThis.X = ...` is
    // unambiguously observably impure: the assignment is
    // visible to anything that reads `globalThis.X` later.
    // Interleaved `globalThis.tag = "..."` writes across two
    // modules close an `S` cycle that the gate must still
    // reject — the relaxation in this PR's main purity-
    // inference work must not over-relax to allow this.
    expect_logical_modules_e2e_rejection_containing_all(
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
#[ignore = "purity inference unimplemented"]
fn inferred_impure_console_log_still_rejected() {
    // `console.log` writes to the host's stdout — observable
    // I/O. A function that calls `console.log` must be
    // classified impure. Top-level statements that invoke
    // such a function across modules must still produce S
    // edges and reject the spec.
    expect_logical_modules_e2e_rejection_containing_all(
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
#[ignore = "purity inference unimplemented"]
fn inferred_impure_via_transitively_impure_callee_still_rejected() {
    // `caller(...)` does nothing observable on its own, but
    // calls `tainted()` which mutates `globalThis`. Recursive
    // purity analysis must propagate impurity from `tainted`
    // back to `caller` and reject specs that distribute
    // `caller(...)` calls across modules in interleaved
    // source order.
    expect_logical_modules_e2e_rejection_containing_all(
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
#[ignore = "spec-level purity declarations unimplemented"]
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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
            json!({
                "id": "logical__mod_a",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_a" },
                "members": [
                    { "id": "m_a", "name": "A", "selector": { "binding": { "name": "A" } } },
                    { "id": "m_c", "name": "C", "selector": { "binding": { "name": "C" } } },
                    {
                        "id": "m_dispatcher",
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
            json!({
                "id": "logical__mod_b",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_b" },
                "members": [
                    { "id": "m_b", "name": "B", "selector": { "binding": { "name": "B" } } },
                ],
            }),
        ],
    ));
    assert_entry_output(&fixture, "alpha beta gamma\n");
}

#[test]
#[ignore = "spec-level purity declarations unimplemented"]
fn declared_pure_member_does_not_bypass_at_init_read_cycle() {
    // Declared purity affects only `S` (side-effect ordering)
    // edges. It does *not* suppress `R` (at-init read) edges.
    // Here `wrap(...)` is annotated pure, but `wrap(B)` in
    // mod_a still reads `B` from mod_b at-init, and `wrap(A)`
    // in mod_b still reads `A` from mod_a at-init: the `R`
    // graph has a cycle and the gate must still reject.
    expect_logical_modules_e2e_rejection_containing_all(
        FixtureOpts::new(
            r#"function wrap(x) { return { ref: x }; }
const A = "a";
const B = wrap(A);
const D = wrap(B);
console.log(B.ref, D.ref);
export { A, B, D, wrap };
"#,
            vec![
                json!({
                    "id": "logical__mod_a",
                    "operation": "define_logical_module",
                    "selector": { "chunkId": "static/app" },
                    "target": { "path": "mod_a" },
                    "members": [
                        { "id": "m_a", "name": "A", "selector": { "binding": { "name": "A" } } },
                        { "id": "m_d", "name": "D", "selector": { "binding": { "name": "D" } } },
                        {
                            "id": "m_wrap",
                            "name": "wrap",
                            "selector": { "binding": { "name": "wrap" } },
                            // Author asserts purity — `S` edges
                            // through `wrap(...)` calls go away.
                            "purity": "pure",
                        },
                    ],
                }),
                json!({
                    "id": "logical__mod_b",
                    "operation": "define_logical_module",
                    "selector": { "chunkId": "static/app" },
                    "target": { "path": "mod_b" },
                    "members": [
                        { "id": "m_b", "name": "B", "selector": { "binding": { "name": "B" } } },
                    ],
                }),
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
#[ignore = "spec-level purity declarations unimplemented"]
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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"function pureWrap(x) { return { val: x }; }
function impureWrap(x) { globalThis.lastWrap = x; return { val: x }; }
const A = pureWrap("a");
const B = pureWrap("b");
const C = pureWrap("c");
console.log(A.val, B.val, C.val);
export { A, B, C, pureWrap, impureWrap };
"#,
        vec![
            json!({
                "id": "logical__mod_a",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_a" },
                "members": [
                    { "id": "m_a", "name": "A", "selector": { "binding": { "name": "A" } } },
                    { "id": "m_c", "name": "C", "selector": { "binding": { "name": "C" } } },
                    {
                        "id": "m_pure_wrap",
                        "name": "pureWrap",
                        "selector": { "binding": { "name": "pureWrap" } },
                        "purity": "pure",
                    },
                    {
                        "id": "m_impure_wrap",
                        "name": "impureWrap",
                        "selector": { "binding": { "name": "impureWrap" } },
                        // No `purity` annotation — default
                        // (impure) classification applies.
                    },
                ],
            }),
            json!({
                "id": "logical__mod_b",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_b" },
                "members": [
                    { "id": "m_b", "name": "B", "selector": { "binding": { "name": "B" } } },
                ],
            }),
        ],
    ));
    assert_entry_output(&fixture, "a b c\n");
}

#[test]
#[ignore = "spec-level purity declarations unimplemented"]
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
    expect_logical_modules_e2e_rejection_containing_all(
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
                json!({
                    "id": "logical__mod_a",
                    "operation": "define_logical_module",
                    "selector": { "chunkId": "static/app" },
                    "target": { "path": "mod_a" },
                    "members": [
                        { "id": "m_a", "name": "A", "selector": { "binding": { "name": "A" } } },
                        { "id": "m_c", "name": "C", "selector": { "binding": { "name": "C" } } },
                        {
                            "id": "m_pure_wrap",
                            "name": "pureWrap",
                            "selector": { "binding": { "name": "pureWrap" } },
                            "purity": "pure",
                        },
                        {
                            "id": "m_impure_wrap",
                            "name": "impureWrap",
                            "selector": { "binding": { "name": "impureWrap" } },
                            // Deliberately unannotated.
                        },
                    ],
                }),
                json!({
                    "id": "logical__mod_b",
                    "operation": "define_logical_module",
                    "selector": { "chunkId": "static/app" },
                    "target": { "path": "mod_b" },
                    "members": [
                        { "id": "m_b", "name": "B", "selector": { "binding": { "name": "B" } } },
                    ],
                }),
            ],
        ),
        &["cycle", "mod_a", "mod_b", "side-effect"],
    );
}
