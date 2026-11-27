//! Pure-function recognition + spec-level purity declarations
//! for the realizability gate's `S` (side-effect ordering)
//! sub-graph.
//!
//! Most tests run in CI. Two remain `#[ignore]`d as desiderata
//! for future work — each ignore-reason names the specific
//! analysis still missing (interprocedural argument freshness for
//! `Object.freeze`, an `Expr::New` whitelist entry for `RegExp`).
//! Their bodies become regression fixtures the moment those
//! analyses land.
//!
//! # Background
//!
//! `classify_expr_purity` (in `purity/`) decides
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
//!    handle. `purity: "pure_new"` is the same kind of trust
//!    contract for `new BoundClass(...)`, while still evaluating
//!    constructor arguments normally.
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
//! `"pure"` and `"pure_new"` are defined here. Future extensions could
//! include `"commutative"` (the function has side effects but
//! they're observably commutative across calls — useful for
//! registry-add patterns where last-write-wins or set-union
//! semantics make ordering irrelevant).
//!
//! The annotation lives at the member level, not on the
//! selector, because it's metadata about the binding, not
//! about how to find it.

use debundle_e2e_support::*;

const MOD_ORDER: &[&str] = &["mod_a", "mod_b", "residual"];

fn members(names: &[&'static str]) -> Vec<Member> {
    names.iter().copied().map(Member::new).collect()
}

/// `source` split between `mod_a` and `mod_b`; `ordered` pins their import order.
fn interleaved<'a>(
    source: &'a str,
    mod_a: &[&'static str],
    mod_b: &[&'static str],
    ordered: bool,
) -> FixtureOpts<'a> {
    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module("mod_a", &members(mod_a)),
            logical_module("mod_b", &members(mod_b)),
        ],
    );
    if ordered {
        opts.with_import_order(MOD_ORDER)
    } else {
        opts
    }
}

fn foo_bar_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("foo", &binding_members(["a", "c"])),
        logical_module("bar", &[Member::new("b")]),
    ]
}

fn pure_new_box() -> [(&'static str, Vec<LogicalModuleEntry>); 1] {
    [(
        "static/vendorlib",
        vec![logical_module(
            "provider",
            &[Member::new("Box").with_purity(MemberPurity::PureNew)],
        )],
    )]
}

/// Asserts `mod_a` owns `A` and `C` and `mod_b` owns `B`.
fn assert_interleaved_ownership(fixture: &Fixture) {
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_a.js",
        &["A", "C"],
        &["B"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &["B"],
        &["A", "C"],
    );
}

// ---------------------------------------------------------------------------
// Inferred purity — `classify_expr_purity` recognizes shapes statically.
// ---------------------------------------------------------------------------

#[test]
fn inferred_pure_initializers_across_modules_emit_no_s_cycle() {
    for (name, source, mod_a, mod_b, ordered, output) in [
        // No global writes, no calls into unknown territory: `wrap(...)` is pure.
        (
            "hof wrapper",
            r#"function wrap(f) { return { kind: "wrapped", impl: f }; }
const A = wrap(function () { return "a"; });
const B = wrap(function () { return "b"; });
const C = wrap(function () { return "c"; });
console.log(A.impl(), B.impl(), C.impl());
export { A, B, C };
"#,
            &["A", "C"][..],
            &["B"][..],
            true,
            "a b c\n",
        ),
        // Reads a function reference off the `Object` global without invoking
        // it; no standard `Object` property is a getter. Logs primitives:
        // Node's array formatting shifts across versions.
        (
            "static Object method aliasing",
            r#"const define = Object.defineProperty;
const freeze = Object.freeze;
const values = Object.values;
const keys = Object.keys;
const obj = freeze({ x: 1 });
console.log(values(obj)[0], keys(obj)[0]);
export { define, freeze, values, keys };
"#,
            &["define", "values"],
            &["freeze", "keys"],
            true,
            "1 x\n",
        ),
        // Minified enum builder: an IIFE mutating and returning its own parameter.
        (
            "IIFE enum builder",
            r#"var Status = ((n) => ((n.OK = 0), (n.WARN = 1), (n.ERROR = 2), n))(Status || {});
var Color = ((n) => ((n.RED = "r"), (n.GREEN = "g"), (n.BLUE = "b"), n))(Color || {});
var Tag = ((n) => ((n[(n.SMALL = 0)] = "small"), (n[(n.LARGE = 1)] = "large"), n))(Tag || {});
console.log(Status.OK, Color.RED, Tag.SMALL, Tag[1]);
export { Status, Color, Tag };
"#,
            &["Status", "Tag"],
            &["Color"],
            false,
            "0 r 0 large\n",
        ),
        // Mutual recursion must terminate and conclude pure. `prev` avoids
        // `n - 1`: arithmetic on an opaque parameter coerces and is NotPure.
        (
            "mutually recursive functions",
            r#"function prev(n) { return n === 4 ? 3 : n === 3 ? 2 : n === 2 ? 1 : 0; }
function even(n) { return n === 0 ? true : odd(prev(n)); }
function odd(n) { return n === 0 ? false : even(prev(n)); }
const fourEven = even(4);
const fiveOdd = odd(5);
console.log(fourEven, fiveOdd);
export { even, odd, fourEven, fiveOdd };
"#,
            &["even", "fourEven"],
            &["odd", "fiveOdd"],
            true,
            "true true\n",
        ),
        // The source-wide write census must prove these `var`s never change
        // before arithmetic can skip user-defined ToPrimitive callbacks.
        (
            "immutable numeric var chain",
            r#"var retry = 20000, delay = 15000, base = 5000, period = base;
var first = delay + period + 2 * retry;
var second = delay + period + 2 * retry;
var third = delay + period + 2 * retry;
console.log(first, second, third);
export { first, second, third };
"#,
            &["retry", "delay", "base", "period", "first", "third"],
            &["second"],
            false,
            "60000 60000 60000\n",
        ),
    ] {
        eprintln!("case: {name}");
        let fixture = run_fixture(interleaved(source, mod_a, mod_b, ordered));
        assert_entry_output(&fixture, output);
    }
}

#[test]
#[ignore = "blocked on interprocedural freshness: `Object.freeze` of a fresh object \
            literal is admitted, but here it freezes `schema`'s parameter, so the \
            call-site literal's freshness must flow into the helper's body"]
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
            logical_module("mod_a", &binding_members(["userSchema", "orderSchema"])),
            logical_module("mod_b", &[Member::new("productSchema")]),
        ],
    ));
    assert_entry_output(&fixture, "user product order\n");
}

#[test]
#[ignore = "blocked on RegExp admission: Set/Map array-literal forms landed (the Set/Map \
            builtin rows; see the new_map / new_set / new_map_with_array_of_pure_pairs tests \
            below), but `new RegExp(\"a+\")` still classifies UnknownNew; its row throws on \
            pattern values until a static ECMA-262 pattern validator exists"]
fn inferred_pure_collection_constructors_with_literal_args_emit_no_s_cycle() {
    // `new Set([...])`, `new Map([...])`, `new RegExp("...")` with
    // literal-only args are pure by built-in catalogue. Set/Map
    // are handled; RegExp is the remaining blocker.
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
            logical_module("mod_a", &binding_members(["tagsA", "cfgA", "reA"])),
            logical_module("mod_b", &binding_members(["tagsB", "cfgB", "reB"])),
        ],
    ));
    assert_entry_output(&fixture, "2 1 1 1 true true\n");
}

// ---------------------------------------------------------------------------
// Inferred purity — negative cases. Real side effects must still register.
// ---------------------------------------------------------------------------

#[test]
fn side_effecting_initializers_across_modules_still_emit_s_cycle() {
    let impure_vendor = [(
        "static/vendorlib",
        "export function wrap(f) { globalThis.__sink = f; return { impl: f }; }\n",
    )];
    let fluent_vendor = [("static/vendorlib", FLUENT_VENDORLIB)];
    for (name, source, mod_a, mod_b, ordered, chunks) in [
        // A call inside the IIFE body is outside the narrow enum-init model.
        (
            "IIFE enum builder calling a helper",
            r#"function value(x) { return x; }
var A = ((n) => (n.OK = value(0), n))(A || {});
var B = ((n) => (n.OK = value(1), n))(B || {});
var C = ((n) => (n.OK = value(2), n))(C || {});
console.log(A.OK, B.OK, C.OK);
export { A, B, C };
"#,
            &["A", "C"][..],
            &["B"][..],
            true,
            &[][..],
        ),
        (
            "console.log in callee",
            r#"function logged(x) { console.log("init:", x); return x; }
const a1 = logged("a1");
const b1 = logged("b1");
const a2 = logged("a2");
console.log(a1, b1, a2);
export { a1, a2, b1 };
"#,
            &["a1", "a2"],
            &["b1"],
            true,
            &[],
        ),
        // Impurity of `tainted` must propagate back to `caller`.
        (
            "transitively impure callee",
            r#"function tainted() { globalThis.touched = true; return 1; }
function caller(label) { tainted(); return { label }; }
const a1 = caller("a1");
const b1 = caller("b1");
const a2 = caller("a2");
console.log(a1.label, b1.label, a2.label, globalThis.touched);
export { a1, a2, b1 };
"#,
            &["a1", "a2"],
            &["b1"],
            true,
            &[],
        ),
        // SOUNDNESS: `obj + 1` runs ToPrimitive, so a user `valueOf` can fire.
        (
            "coercing operator on opaque operand",
            r#"const a1 = obj + 1;
const b1 = obj + 2;
const a2 = obj + 3;
console.log(a1, b1, a2);
export { a1, a2, b1 };
"#,
            &["a1", "a2"],
            &["b1"],
            false,
            &[],
        ),
        // SOUNDNESS: `const { a1 } = src` fires `src`'s getters.
        (
            "destructuring declarators",
            r#"const src = make();
const { a1 } = src;
const { b1 } = src;
const { a2 } = src;
console.log(a1, b1, a2);
export { a1, a2, b1 };
"#,
            &["a1", "a2"],
            &["b1"],
            true,
            &[],
        ),
        // The cross-module oracle removes only false impurity, never real effects.
        (
            "impure imported wrapper",
            r#"import { wrap } from "./vendorlib.js";
const A = wrap(function () { return "a"; });
const B = wrap(function () { return "b"; });
const C = wrap(function () { return "c"; });
console.log(A, B, C);
export { A, B, C };
"#,
            &["A", "C"],
            &["B"],
            false,
            &impure_vendor,
        ),
        // Fluent chain receivers are call results, so every link is
        // `unknown_call`/`unknown_member` without an assertion.
        (
            "fluent chain schema declarations without assertion",
            FLUENT_CHAIN_ENTRY,
            &["A", "C"],
            &["B"],
            true,
            &fluent_vendor,
        ),
    ] {
        eprintln!("case: {name}");
        expect_sequenced_cycle_rejection(
            interleaved(source, mod_a, mod_b, ordered).with_extra_chunks(chunks),
            &["mod_a", "mod_b"],
        );
    }
}

// ---------------------------------------------------------------------------
// Declared purity — author asserts purity via `member.purity = "pure"`.
// ---------------------------------------------------------------------------

fn dispatcher_fixture(dispatcher: Member) -> FixtureOpts<'static> {
    FixtureOpts::new(
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
            logical_module("mod_a", &[Member::new("A"), Member::new("C"), dispatcher]),
            logical_module("mod_b", &[Member::new("B")]),
        ],
    )
    .with_import_order(MOD_ORDER)
}

#[test]
fn declared_pure_member_suppresses_s_edges_for_opaque_call() {
    // Inference cannot prove the dynamic registry lookup pure; the author's
    // `purity: "pure"` is trusted.
    let fixture = run_fixture(dispatcher_fixture(
        Member::new("dispatcher").with_purity(MemberPurity::Pure),
    ));
    assert_entry_output(&fixture, "alpha beta gamma\n");
}

#[test]
fn declared_pure_member_does_not_bypass_at_init_read_cycle() {
    // Declared purity suppresses only `S` edges: `D` reads `B` and `B` reads
    // `A` at init, an `R` cycle between the modules.
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"function wrap(x) { return { ref: x }; }
const A = "a";
const B = wrap(A);
const D = wrap(B);
console.log(B.ref, D.ref);
export { A, B, D, wrap };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("A"),
                        Member::new("D"),
                        Member::new("wrap").with_purity(MemberPurity::Pure),
                    ],
                ),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn canonical_source_match_purity_annotation_contributes_to_analysis_inputs() {
    // `dispatcher` calls through a registry lookup, which inference cannot
    // prove pure, so the `purity` on its source_match member is the only
    // thing that drops the `S` edges of the interleaved call sites. The
    // unannotated control keeps that premise from silently going vacuous.
    let dispatcher = || {
        Member::source_alpha(
            "dispatcher",
            r#"function dispatcher(req) { return registry[req.kind](req.payload); }"#,
        )
    };
    expect_rejection_containing_all(
        dispatcher_fixture(dispatcher()),
        &["cycle", "mod_a", "mod_b", "side-effect"],
    );
    let fixture = run_fixture(dispatcher_fixture(
        dispatcher().with_purity(MemberPurity::Pure),
    ));
    assert_entry_output(&fixture, "alpha beta gamma\n");
}

#[test]
fn declared_pure_annotation_applies_only_to_annotated_member_negative() {
    // The `pureWrap` annotation must not bleed onto the unannotated,
    // global-writing `impureWrap`.
    expect_sequenced_cycle_rejection(
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
                logical_module(
                    "mod_a",
                    &[
                        Member::new("A"),
                        Member::new("C"),
                        Member::new("pureWrap").with_purity(MemberPurity::Pure),
                        Member::new("impureWrap"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn declared_pure_new_member_suppresses_s_edges_but_still_evaluates_arguments() {
    let pure_box_modules = || {
        vec![
            logical_module(
                "mod_a",
                &[
                    Member::new("A"),
                    Member::new("C"),
                    Member::new("PureBox").with_purity(MemberPurity::PureNew),
                ],
            ),
            logical_module("mod_b", &[Member::new("B")]),
        ]
    };
    let fixture = run_fixture(FixtureOpts::new(
        r#"class PureBox {
  constructor(value) {
    this.value = value;
  }
}
const A = new PureBox("a");
const B = new PureBox("b");
const C = new PureBox("c");
console.log(A.value, B.value, C.value);
export { A, B, C, PureBox };
"#,
        pure_box_modules(),
    ));
    assert_entry_output(&fixture, "a b c\n");
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            r#"class PureBox {
  constructor(value) {
    this.value = value;
  }
}
function value(label) {
  globalThis.last = label;
  return label;
}
const A = new PureBox(value("a"));
const B = new PureBox(value("b"));
const C = new PureBox(value("c"));
console.log(A.value, B.value, C.value, globalThis.last);
export { A, B, C, PureBox };
"#,
            pure_box_modules(),
        )
        .with_import_order(MOD_ORDER),
        &["mod_a", "mod_b"],
    );
}

// ---------------------------------------------------------------------------
// Pure initializers peeled away from a side-effecting predecessor.
// ---------------------------------------------------------------------------

// Cycle-forcing fixture: `b` is peeled to its own module while the preceding
// side-effecting IIFE `a` and a later at-init reader `c` stay in residual. Were
// `b` side-effecting, its S edge to `a` plus `c`'s read of `b` would cycle.
//
// `new Map()`/`Set()`/`WeakMap()`/`Array()` allocate without user code. For
// `Set`/`Map`, an Array-literal argument of pure elements without spreads runs
// only the built-in iterator and primitive-key `add`/`set`.
#[test]
fn builtin_container_initializers_break_the_side_effect_cycle() {
    for (initializer, read, output) in [
        ("new Map()", "b.size", "1\n"),
        ("new Set()", "b.size", "1\n"),
        ("new WeakMap()", "(b ? \"y\" : \"n\")", "y1\n"),
        ("new Array()", "b.length", "1\n"),
        (r#"new Set(["x", "y", "z"])"#, "b.size", "4\n"),
        (r#"new Map([["x", 1], ["y", 2]])"#, r#"b.get("x")"#, "2\n"),
    ] {
        eprintln!("case: {initializer}");
        let declaration = format!("const b = {initializer};");
        let source = format!(
            "const a = (() => 1)();\n{declaration}\nconst c = {read} + a;\nconsole.log(c);\nexport {{ a, b, c }};"
        );
        assert_pure_cycle_break_with_opts(
            FixtureOpts::new(
                &source,
                vec![logical_module("b_module", &[Member::new("b")])],
            )
            .with_import_order(&["b_module", "residual"]),
            "b_module",
            &[declaration.split_inclusive('(').next().unwrap()],
            &["const a"],
            output,
        );
    }
}

#[test]
fn pure_declarations_break_the_side_effect_cycle() {
    for (name, source, module, owned, present, absent, output) in [
        // `static x = new Map()` is the class's only static-side candidate.
        (
            "class static new Map field",
            r#"const a = (() => 1)();
class C {
  static x = new Map();
}
const c = C.x.size + a;
console.log(c);
export { a, C, c };
"#,
            "c_module",
            &["C"][..],
            &["class C", "static x = new Map()"][..],
            &["const a"][..],
            "1\n",
        ),
        (
            "immutable symbol class key",
            r#"const before = (() => 1)();
const brand = Symbol('brand');
class Hidden { [brand]() { return 7; } }
const after = Hidden.name + before;
console.log(after);
export { Hidden, after };
"#,
            "class/hidden",
            &["brand", "Hidden"],
            &["class Hidden", "brand"],
            &["const before"],
            "Hidden1\n",
        ),
        // ECMA-262 §20.4.1.1: `ToString` of a primitive-literal description
        // runs no user code.
        (
            "Symbol with string literal",
            r#"const a = (() => 1)();
const b = Symbol("b");
const c = b.description + a;
console.log(c);
export { a, b, c };
"#,
            "b_module",
            &["b"],
            &["const b = Symbol(", "export {", "b"],
            &["const a", "(()=>1)"],
            "b1\n",
        ),
        // §20.4.1.1 step 2: no description, no `ToString`.
        (
            "Symbol without arguments",
            r#"const a = (() => 1)();
const b = Symbol();
const c = (typeof b) + a;
console.log(c);
export { a, b, c };
"#,
            "b_module",
            &["b"],
            &["const b = Symbol()", "export {", "b"],
            &["const a"],
            "symbol1\n",
        ),
        (
            "String.raw template",
            r#"const a = (() => 1)();
const b = String.raw`value:${1}`;
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
            "b_module",
            &["b"],
            &["const b = String.raw"],
            &["const a"],
            "8\n",
        ),
    ] {
        eprintln!("case: {name}");
        assert_pure_cycle_break_with_opts(
            FixtureOpts::new(source, vec![logical_module(module, &members(owned))])
                .with_import_order(&[module, "residual"]),
            module,
            present,
            absent,
            output,
        );
    }
}

// `recv?.prop` / `recv?.()` add only a null/undefined short-circuit, so purity
// recurses through the chain: a whitelisted static read stays `Pure`. Host-global
// receivers (`window?.X?.Y`) are not whitelisted.
#[test]
fn optional_chain_on_whitelisted_receiver_classified_pure() {
    // Were `Y` side-effecting, its S edge to the IIFE `X` plus `Z`'s at-init
    // read of `Y` would cycle `y_module` with residual.
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const X = (() => "x")();
const Y = Number?.MAX_SAFE_INTEGER;
const Z = Y + 1;
console.log(Z);
export { X, Y, Z };
"#,
            vec![logical_module("y_module", &[Member::new("Y")])],
        )
        .with_import_order(&["y_module", "residual"]),
    );
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
    assert_entry_output(&fixture, "9007199254740992\n");
}

// ---------------------------------------------------------------------------
// Imported purity.
// ---------------------------------------------------------------------------

#[test]
fn cross_module_pure_import_emits_no_s_cycle() {
    let vendor = (
        "static/vendorlib",
        "export function wrap(f) { return { kind: \"wrapped\", impl: f }; }\n",
    );
    for (name, import, chunks) in [
        // The program-level oracle resolves the imported `wrap` as pure.
        (
            "direct import",
            r#"import { wrap } from "./vendorlib.js";"#,
            vec![vendor],
        ),
        // A direct-import-only pass leaves `callWrap` opaque; the static
        // import closure must include the adapter and its vendor dependency.
        (
            "transitive static import closure",
            r#"import { callWrap as wrap } from "./adapter.js";"#,
            vec![
                (
                    "static/adapter",
                    "import { wrap } from \"./vendorlib.js\";\n\
export function callWrap(f) { return wrap(f); }\n",
                ),
                vendor,
            ],
        ),
    ] {
        eprintln!("case: {name}");
        let source = format!(
            r#"{import}
const A = wrap(function () {{ return "a"; }});
const B = wrap(function () {{ return "b"; }});
const C = wrap(function () {{ return "c"; }});
console.log(JSON.stringify([[A.kind, A.impl()], [B.kind, B.impl()], [C.kind, C.impl()]]));
export {{ A, B, C }};
"#
        );
        let fixture = run_fixture(
            interleaved(&source, &["A", "C"], &["B"], false).with_extra_chunks(&chunks),
        );
        assert_preserved_entry_output(
            &fixture,
            "[[\"wrapped\",\"a\"],[\"wrapped\",\"b\"],[\"wrapped\",\"c\"]]\n",
        );
        assert_interleaved_ownership(&fixture);
    }
}

#[test]
fn declared_imported_constructor_purity_emits_no_s_cycle() {
    for (import, provider) in [
        (
            "import { Box as Provider } from './vendorlib.js';",
            "export class Box { constructor(value) { this.value = value; } }\n",
        ),
        (
            "import Provider from './vendorlib.js';",
            "class Box { constructor(value) { this.value = value; } }\nexport { Box as default };\n",
        ),
    ] {
        eprintln!("case: {import}");
        let source = format!(
            r#"{import}
const a = new Provider(1);
const b = new Provider(2);
const c = new Provider(3);
console.log(a.value, b.value, c.value);
export {{ a, b, c }};
"#
        );
        let fixture = run_fixture(
            FixtureOpts::new(&source, foo_bar_modules())
                .with_extra_chunks(&[("static/vendorlib", provider)])
                .with_extra_chunk_logical_modules(&pure_new_box()),
        );
        assert_entry_output(&fixture, "1 2 3\n");
    }
}

#[test]
fn declared_imported_constructor_purity_resolves_aliased_source_path() {
    const ENTRY: &str = r#"import { Provider as Factory } from "/$bunfs/root/provider.js";
const a = new Factory(1);
const b = new Factory(2);
const c = new Factory(3);
console.log(a.value, b.value, c.value);
export { a, b, c };
"#;
    const PROVIDER: &str = "export class Provider { constructor(value) { this.value = value; } }\n";
    let provider_modules = [(
        "static/provider",
        vec![logical_module(
            "provider",
            &[Member::new("Provider").with_purity(MemberPurity::PureNew)],
        )],
    )];
    // Listing the physical file and its copy as independent chunks makes
    // the assertion miss the actual import target, reproducing the reported
    // unknown_new cycle without any private source.
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(ENTRY, foo_bar_modules())
            .with_extra_chunks(&[
                ("$bunfs/root/provider", PROVIDER),
                ("static/provider", PROVIDER),
            ])
            .with_extra_chunk_logical_modules(&provider_modules),
        &["foo", "bar"],
    );
    let fixture = run_fixture(
        FixtureOpts::new(ENTRY, foo_bar_modules())
            .with_extra_chunks(&[("static/provider", PROVIDER)])
            .with_source_paths(&[
                ("static/app", "$bunfs/root/app.js"),
                ("static/provider", "$bunfs/root/provider.js"),
            ])
            .with_extra_chunk_logical_modules(&provider_modules),
    );
    assert_entry_output(&fixture, "1 2 3\n");
}

#[test]
fn source_selected_constructor_purity_reaches_importer() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { A as Factory } from "./provider.js";
const a = new Factory(1);
const b = new Factory(2);
const c = new Factory(3);
console.log(a.value, b.value, c.value);
export { a, b, c };
"#,
            foo_bar_modules(),
        )
        .with_extra_chunks(&[(
            "static/provider",
            "export class A { #value; constructor(value) { this.#value = value; } get value() { return this.#value; } }\n",
        )])
        .with_extra_chunk_logical_modules(&[(
            "static/provider",
            vec![logical_module(
                "provider",
                &[Member::source_alpha(
                    "Provider",
                    "export class Provider { #value; constructor(value) { this.#value = value; } get value() { return this.#value; } }",
                )
                .with_purity(MemberPurity::PureNew)],
            )],
        )]),
    );
    assert_entry_output(&fixture, "1 2 3\n");
}

#[test]
fn imported_constructor_side_effects_keep_s_cycle() {
    for (name, source, vendor, declared) in [
        // A write through `this` can invoke a class-prototype setter, so a
        // constructor is not inferred pure from its assignment shape.
        (
            "setter-invoking constructor",
            r#"import { Box as Provider } from "./vendorlib.js";
const a = new Provider(1);
const b = new Provider(2);
const c = new Provider(3);
export { a, b, c };
"#,
            "export class Box { set value(v) { globalThis.seen = v; } constructor(v) { this.value = v; } }\n",
            false,
        ),
        (
            "declared pure_new still classifies arguments",
            r#"import { Box as Provider } from "./vendorlib.js";
const a = new Provider(console.log("a"));
const b = new Provider(console.log("b"));
const c = new Provider(console.log("c"));
export { a, b, c };
"#,
            "export class Box { constructor(value) { this.value = value; } }\n",
            true,
        ),
        (
            "declared pure_new does not make plain calls pure",
            r#"import { Box as Provider } from "./vendorlib.js";
const a = Provider(1);
const b = Provider(2);
const c = Provider(3);
export { a, b, c };
"#,
            "export class Box { constructor(value) { this.value = value; } }\n",
            true,
        ),
    ] {
        eprintln!("case: {name}");
        let declarations = pure_new_box();
        let chunks = [("static/vendorlib", vendor)];
        let opts = FixtureOpts::new(source, foo_bar_modules()).with_extra_chunks(&chunks);
        let opts = if declared {
            opts.with_extra_chunk_logical_modules(&declarations)
        } else {
            opts
        };
        expect_sequenced_cycle_rejection(opts, &["foo", "bar"]);
    }
}

#[test]
fn asserted_pure_namespace_member_calls_emit_no_s_cycle() {
    // CJS-interop shape: `ns.make(...)` on an opaque namespace import. A
    // binding-level `pure_members` annotation on the importer's `ns` admits it.
    let fixture = run_fixture(
        interleaved(
            r#"import { reactish as ns } from "./vendorlib.js";
const A = ns.make(function () { return "a"; });
const B = ns.make(function () { return "b"; });
const C = ns.make(function () { return "c"; });
console.log(A.impl(), B.impl(), C.impl());
export { A, B, C };
"#,
            &["A", "C"],
            &["B"],
            false,
        )
        .with_extra_chunks(&[(
            "static/vendorlib",
            "const reactish = { make(f) { return { impl: f }; } };\nexport { reactish };\n",
        )])
        .with_chunk_renames(chunk_rename_with_pure_members(
            "ns",
            "ns",
            Some("import_specifier"),
            &["make"],
        )),
    );
    assert_preserved_entry_output(&fixture, "a b c\n");
    assert_interleaved_ownership(&fixture);
}

/// Zod-style schema-declaration entry: every statement is a fluent
/// chain whose intermediate receivers are call RESULTS
/// (`k.object({...}).describe(...)`), plus one `const`-derived root
/// (`Base` → `.extend(...)`). No binding-keyed purity surface
/// (`pure_exports`, `pure_members`, `imported_purities`) can admit
/// link 2+ of these chains — there is no binding to key on.
const FLUENT_CHAIN_ENTRY: &str = r#"import { e4 as k } from "./vendorlib.js";
const Base = k.object({ id: 1 });
const A = k.object({ a: 1 }).describe("a");
const B = Base.extend({ b: 2 }).describe("b");
const C = k.object({ c: 1 }).describe("c");
console.log(A.shape.a, A.d, B.shape.id, B.shape.b, B.d, C.shape.c, C.d);
export { A, B, C };
"#;

const FLUENT_VENDORLIB: &str = "const zodish = { object(shape) { return { shape, \
describe(d) { return { shape: this.shape, d }; }, extend(more) { return zodish.object({ ...this.shape, ...more }); \
} }; } };\nexport { zodish as e4 };\n";

fn fluent_e4_assertion() -> [(&'static str, ChunkExportPurity); 1] {
    [ChunkExportPurityBuilder::new("static/vendorlib")
        .with_fluent_exports(&["e4"])
        .build()]
}

#[test]
fn asserted_fluent_export_chains_emit_no_s_cycle() {
    // A definition-side `fluent_exports` assertion projects onto the
    // importer's `k`; every link, including the `Base`-derived `.extend`
    // chain, classifies pure. The unasserted baseline is in
    // `side_effecting_initializers_across_modules_still_emit_s_cycle`.
    let purities = fluent_e4_assertion();
    let fixture = run_fixture(
        interleaved(FLUENT_CHAIN_ENTRY, &["A", "C"], &["B"], true)
            .with_extra_chunks(&[("static/vendorlib", FLUENT_VENDORLIB)])
            .with_chunk_export_purity(&purities),
    );
    assert_preserved_entry_output(&fixture, "1 a 1 2 b 1 c\n");
    assert_interleaved_ownership(&fixture);
}

#[test]
fn fluent_export_assertion_keeps_unasserted_effects() {
    let sibling_vendor = format!(
        "{FLUENT_VENDORLIB}\nexport const sibling = {{ object(shape) {{ console.log(shape.value); return zodish.object(shape); }} }};\n",
    );
    let purities = fluent_e4_assertion();
    for (name, source, vendor) in [
        (
            "argument effects",
            r#"import { e4 as k } from "./vendorlib.js";
const A = k.object({ value: 1 }).describe((console.log("a"), "a"));
const B = k.object({ value: 2 }).describe((console.log("b"), "b"));
const C = k.object({ value: 3 }).describe((console.log("c"), "c"));
console.log(A.d, B.d, C.d);
export { A, B, C };
"#,
            FLUENT_VENDORLIB,
        ),
        (
            "sibling export",
            r#"import { sibling } from "./vendorlib.js";
const A = sibling.object({ value: "a" }).describe("a");
const B = sibling.object({ value: "b" }).describe("b");
const C = sibling.object({ value: "c" }).describe("c");
console.log(A.d, B.d, C.d);
export { A, B, C };
"#,
            sibling_vendor.as_str(),
        ),
    ] {
        eprintln!("case: {name}");
        let chunks = [("static/vendorlib", vendor)];
        let fixture = run_fixture(
            FixtureOpts::new(
                source,
                vec![logical_module("values", &binding_members(["A", "B", "C"]))],
            )
            .with_extra_chunks(&chunks)
            .with_chunk_export_purity(&purities),
        );
        assert_preserved_entry_output(&fixture, "a\nb\nc\na b c\n");
        expect_sequenced_cycle_rejection(
            interleaved(source, &["A", "C"], &["B"], false)
                .with_extra_chunks(&chunks)
                .with_chunk_export_purity(&purities),
            &["mod_a", "mod_b"],
        );
    }
}

// ---------------------------------------------------------------------------
// Local property effects — `displayName`-style annotation writes.
// ---------------------------------------------------------------------------

/// React annotation idiom: each component declaration is followed by
/// a `displayName` data-property write. The writes are anonymous
/// statements (no declared bindings) whose only effect is on the
/// component binding directly above them.
const ANNOTATION_WRITE_ENTRY: &str = r#"function A() { return 1; }
A.displayName = "A";
function B() { return 2; }
B.displayName = "B";
function C() { return 3; }
C.displayName = "C";
console.log(JSON.stringify([[A.displayName, A()], [B.displayName, B()], [C.displayName, C()]]));
export { A, B, C };
"#;

/// Each component goes to its module with its annotation write.
fn annotation_write_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module_with_anon(
            "mod_a",
            &binding_members(["A", "C"]),
            &[r#"A.displayName = "A";"#, r#"C.displayName = "C";"#],
        ),
        logical_module_with_anon("mod_b", &[Member::new("B")], &[r#"B.displayName = "B";"#]),
    ]
}

#[test]
fn annotation_property_writes_without_policy_emit_s_cycle() {
    // Default policy: every `X.displayName = …` is an impure
    // `assign_or_update`, so source order cycles mod_a → mod_b → mod_a.
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(ANNOTATION_WRITE_ENTRY, annotation_write_modules()),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn annotation_property_writes_with_policy_co_locate_and_realize() {
    // With the policy, each write is Pure with a LocalEffect on its component.
    let fixture = run_fixture(
        FixtureOpts::new(ANNOTATION_WRITE_ENTRY, annotation_write_modules())
            .with_local_property_effects(),
    );
    assert_preserved_entry_output(&fixture, "[[\"A\",1],[\"B\",2],[\"C\",3]]\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_a.js",
        &[
            "function A()",
            "function C()",
            "A.displayName = \"A\";",
            "C.displayName = \"C\";",
        ],
        &["function B()", "B.displayName ="],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &["function B()", "B.displayName = \"B\";"],
        &[
            "function A()",
            "function C()",
            "A.displayName =",
            "C.displayName =",
        ],
    );
}

#[test]
fn annotation_property_write_cannot_be_split_from_its_declarer() {
    // The policy scopes the effect, it does not erase it: the LocalEffect
    // edge forces the write into its target's module.
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            ANNOTATION_WRITE_ENTRY,
            vec![
                logical_module("mod_a", &binding_members(["A", "C"])),
                logical_module_with_anon(
                    "mod_b",
                    &[Member::new("B")],
                    &[r#"A.displayName = "A";"#],
                ),
            ],
        )
        .with_local_property_effects(),
        &["mod_a", "mod_b"],
        &["local_effect"],
    );
}
