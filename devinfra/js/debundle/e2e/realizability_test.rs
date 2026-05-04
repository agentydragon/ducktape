//! Realizability gate (`I ∪ S` per <DESIGN.md>): the
//! materializer accepts a spec and emits a behaviour-preserving
//! bundle iff the imports graph plus side-effect ordering is
//! acyclic. Each test feeds a fixture spec and asserts either
//! acceptance + entry-stdout match, or rejection with cycle
//! evidence naming the implicated modules.

use debundle_e2e_support::*;
use serde_json::json;

// --- R cycles (both back-edges at-init) ----------------------------------

#[test]
fn cyclic_spec_is_rejected_with_clear_error() {
    // mod_x = {A, D}: D = wrap(C) reads C from mod_y.
    // mod_y = {B, C}: B = wrap(A) reads A from mod_x.
    // Cycle: mod_x ↔ mod_y.
    let opts = FixtureOpts::new(
        r#"function wrap(x) { return { ref: x }; }
const A = "a";
const B = wrap(A);
const C = "c";
const D = wrap(C);
console.log(B.ref, D.ref);
export { A, B, C, D };
"#,
        vec![
            (
                "mod_x".to_string(),
                json!({
                    "id": "logical__mod_x",
                    "members": [
                        { "name": "A", "selector": { "binding": { "name": "A" } } },
                        { "name": "D", "selector": { "binding": { "name": "D" } } },
                    ],
                }),
            ),
            (
                "mod_y".to_string(),
                json!({
                    "id": "logical__mod_y",
                    "members": [
                        { "name": "B", "selector": { "binding": { "name": "B" } } },
                        { "name": "C", "selector": { "binding": { "name": "C" } } },
                    ],
                }),
            ),
        ],
    );
    expect_logical_modules_e2e_rejection(opts, &["cycle", "mod_x", "mod_y"]);
}

// --- I cycles via lazy back-edges ----------------------------------------

#[test]
fn rejects_cycle_through_lazy_back_edge() {
    // mod_b reads A from mod_a at-init (B's initializer);
    // mod_a's `readB` body reads B from mod_b lazily. `R` is
    // acyclic by itself, but the SCC `{mod_a, mod_b}` in `I ∪ S`
    // contains the at-init `mod_b → mod_a` edge — that single
    // `R` cross-module edge inside the SCC is enough for the
    // realizability gate to bail. (Without the bail the linker
    // would TDZ on B's initializer.)
    expect_logical_modules_e2e_rejection_containing_all(
        FixtureOpts::new(
            r#"const A = "a-value";
function readB() { return B; }
const B = A + "-postfix";
console.log(readB());
export { A, B, readB };
"#,
            vec![
                logical_module("mod_a", &[Member::new("A"), Member::new("readB")]),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["cycle", "mod_a", "mod_b"],
    );
}

#[test]
fn accepts_cycle_when_all_back_edges_are_lazy() {
    // mod_a owns A and readB; readB() lazily reads B.
    // mod_b owns B and readA; readA() lazily reads A.
    // No cross-module read fires at-init: when the linker
    // evaluates either module, no top-level statement reaches
    // into the other one. The function bodies only run *after*
    // both modules finish evaluating — no TDZ.
    //
    // The SCC in the imports graph `I` is `{mod_a, mod_b}`, but
    // it carries no at-init (`R`) and no side-effect (`S`)
    // cross-module edges — only `L` edges. The realizability
    // gate must accept this spec; rejecting would over-restrict
    // the realizable subset of `I ∪ S` cycles.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const A = "a-value";
const B = "b-value";
function readA() { return A; }
function readB() { return B; }
console.log(readA(), readB());
export { A, B, readA, readB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("A"), Member::new("readB")]),
            logical_module("mod_b", &[Member::new("B"), Member::new("readA")]),
        ],
    ));
    // ESM evaluates both modules to completion before the
    // residual entry's `console.log(readA(), readB())` fires;
    // both function bodies see fully-assigned bindings.
    assert_entry_output(&fixture, "a-value b-value\n");
}

// --- Acyclic specs and cross-module init order ----------------------------

#[test]
fn init_call_order_respects_cross_module_dependency() {
    // mod_a owns x1 + x2 (x2 reads y.id at-init); mod_b owns y.
    // R-edge mod_a → mod_b; ESM linker evaluates mod_b first.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const x1 = { id: "x1" };
const y = { id: "k" };
const x2 = { [y.id]: "v" };
console.log(x1.id, y.id, x2[y.id]);
export { x1, y, x2 };
"#,
        vec![
            (
                "mod_a".to_string(),
                json!({
                    "id": "logical__mod_a",
                    "members": [
                        { "name": "x1", "selector": { "binding": { "name": "x1" } } },
                        { "name": "x2", "selector": { "binding": { "name": "x2" } } },
                    ],
                }),
            ),
            (
                "mod_b".to_string(),
                json!({
                    "id": "logical__mod_b",
                    "members": [
                        { "name": "y", "selector": { "binding": { "name": "y" } } },
                    ],
                }),
            ),
        ],
    ));
    assert_entry_output(&fixture, "x1 k v\n");
}

// --- Side-effect ordering (`S`) ------------------------------------------

#[test]
fn pure_const_decls_across_modules_dont_create_s_cycles() {
    // Pure literal initializers across mod_a/mod_b in interleaved
    // source order. A coarse `has_side_effect` would generate S
    // edges in both directions; `classify_expr_purity` sees these
    // as Pure and S stays empty.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const a1 = 1;
const b1 = 2;
const a2 = "x";
const b2 = "y";
const a3 = { k: a1 };
const b3 = [b1, b2];
console.log(a1, a2, a3.k, b1, b2, b3[0]);
export { a1, a2, a3, b1, b2, b3 };
"#,
        vec![
            logical_module(
                "mod_a",
                &[Member::new("a1"), Member::new("a2"), Member::new("a3")],
            ),
            logical_module(
                "mod_b",
                &[Member::new("b1"), Member::new("b2"), Member::new("b3")],
            ),
        ],
    ));
    assert_entry_output(&fixture, "1 x 1 2 y 2\n");
}

#[test]
fn s_only_cycle_is_rejected() {
    // Three side-effecting `globalThis.tag = ...` writes
    // interleaved across mod_a (ord 0, 2) and mod_b (ord 1). No
    // R/I edges; S alone closes the cycle.
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
        // `side-effect` substring confirms the rejection comes
        // from S edges, not from a misclassified R/I edge.
        &["cycle", "mod_a", "mod_b", "side-effect"],
    );
}

// --- Per-declarator attribution across comma-list var-decls --------------

#[test]
fn comma_list_split_does_not_invent_cross_module_cycle() {
    // Without per-declarator attribution, `stmt_owner` picks A's
    // owner (mod_x) for the whole `const A = 1, B = X` and
    // attributes B's read of X to mod_x — inventing an
    // mod_x → mod_y edge that, combined with the real
    // mod_y → mod_x edge from `Y = a_in_x`, closes a cycle.
    // Pre-analysis split: B's row stands alone; X is in mod_y
    // (same module); no spurious edge.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const a_in_x = "x";
const X = 42;
const A = 1, B = X;
const Y = a_in_x;
console.log(A, B, X, Y);
export { A, B, X, Y, a_in_x };
"#,
        vec![
            logical_module("mod_x", &[Member::new("A"), Member::new("a_in_x")]),
            logical_module(
                "mod_y",
                &[Member::new("B"), Member::new("X"), Member::new("Y")],
            ),
        ],
    ));
    // Original chunk evaluates left-to-right: a_in_x="x", X=42,
    assert_entry_output(&fixture, "1 42 42 x\n");
}

#[test]
fn comma_list_split_surfaces_missed_cross_module_cycle() {
    // Real cycle: `B = a_in_x` in mod_y reads mod_x; `fromB = B`
    // in mod_x reads mod_y. Without per-declarator attribution,
    // pre-fix charges `a_in_x`'s read to mod_x (A's owner) — the
    // mod_y → mod_x edge disappears and validator accepts a spec
    // that TDZs at runtime. Pre-analysis split keeps B's row
    // attributed to mod_y; cycle re-surfaces.
    expect_logical_modules_e2e_rejection_containing_all(
        FixtureOpts::new(
            r#"const a_in_x = "x";
const A = 1, B = a_in_x;
const fromB = B;
console.log(A, B, fromB);
export { A, B, fromB, a_in_x };
"#,
            vec![
                logical_module(
                    "mod_x",
                    &[
                        Member::new("A"),
                        Member::new("a_in_x"),
                        Member::new("fromB"),
                    ],
                ),
                logical_module("mod_y", &[Member::new("B")]),
            ],
        ),
        &["cycle", "mod_x", "mod_y"],
    );
}

// --- Top-level await is rejected (DESIGN.md A2) --------------------------

#[test]
fn top_level_await_is_rejected() {
    // `await` at module-top isn't covered by the realizability
    // theorem (A2). The materializer rejects before fact analysis
    // runs.
    expect_logical_modules_e2e_rejection_containing_all(
        FixtureOpts::new(
            r#"async function fetchData() { return 42; }
const value = await fetchData();
console.log(value);
export { value };
"#,
            vec![logical_module("mod_x", &[Member::new("value")])],
        ),
        &["top-level", "await", "TLA"],
    );
}

#[test]
fn await_inside_async_function_is_allowed() {
    // `await` inside an async function body is fine — the lazy
    // boundary keeps the module synchronous.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"async function fetchData() {
  return await Promise.resolve(42);
}
const promise = fetchData();
promise.then((v) => console.log(v));
export { fetchData, promise };
"#,
        vec![logical_module(
            "mod_x",
            &[Member::new("fetchData"), Member::new("promise")],
        )],
    ));
    assert_entry_output(&fixture, "42\n");
}

#[test]
fn await_in_instance_class_field_is_allowed() {
    // Instance field initializers run on `new`, not at class-decl
    // time. An `await` there is *not* top-level. (Per spec the
    // host method must be `async` for the `await` to be syntactically
    // valid; we use a lazy method here.)
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"class C {
  async run() { return await Promise.resolve("ok"); }
}
const c = new C();
c.run().then((v) => console.log(v));
export { C };
"#,
        vec![logical_module("mod_x", &[Member::new("C")])],
    ));
    assert_entry_output(&fixture, "ok\n");
}

#[test]
fn await_in_static_class_field_is_rejected() {
    // Static field initializers run at class-decl time. If the
    // class is at module-top, an `await` in a static field is
    // top-level. Wrap the await in an IIFE — `static x = await …`
    // is a SyntaxError outside async contexts, but the visitor
    // rejects any reachable AwaitExpr regardless of whether the
    // surrounding host is well-formed.
    expect_logical_modules_e2e_rejection_containing_all(
        FixtureOpts::new(
            r#"async function f() { return 1; }
class C {
  static x = (async () => await f())();
  static y = await f();
}
console.log(C.x, C.y);
export { C };
"#,
            vec![logical_module(
                "mod_x",
                &[Member::new("C"), Member::new("f")],
            )],
        ),
        &["top-level", "await", "TLA"],
    );
}

#[test]
fn await_in_computed_class_method_key_is_rejected() {
    // Computed property keys are evaluated at class-decl time
    // (eager) regardless of `is_static`. An `await` there is
    // top-level.
    expect_logical_modules_e2e_rejection_containing_all(
        FixtureOpts::new(
            r#"async function k() { return "m"; }
class C {
  [await k()]() {}
}
console.log(C);
export { C };
"#,
            vec![logical_module(
                "mod_x",
                &[Member::new("C"), Member::new("k")],
            )],
        ),
        &["top-level", "await", "TLA"],
    );
}
