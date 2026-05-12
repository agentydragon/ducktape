//! End-to-end coverage for per-declarator owner split on top-level
//! comma-lists.
//!
//! A top-level `var/let/const a = 1, b = 2;` declares two
//! independent bindings. A spec that names `a` in `mod_a` and `b`
//! in `mod_b` must materialize each declarator into its own
//! destination module — splitting the comma-list at lower-time —
//! while preserving:
//!
//! - source-order side effects (sibling RHS that mutates state
//!   must run in the same observable order),
//! - the original declaration kind (`const` / `let` / `var`)
//!   on each side,
//! - the `export` directive when the input was `export const a = 1, b = 2;`,
//! - cross-sibling reads (one sibling reads the other; the materializer
//!   must emit an `import` between the two modules).
//!
//! Companion regression: <sibling_declarator_steal_test.rs> pins
//! the bug where the closure-pass was overwriting binding
//! assignments for explicit-claim siblings. This file pins
//! the spec-side contract (every shape of comma-list a YAML
//! peel can address by name resolves to one declarator each)
//! and runs the result under `node` to catch behavioural
//! regressions, not just emission-shape regressions.

use debundle_e2e_support::*;
use std::fs;

// --- Helpers --------------------------------------------------------------

fn read(path_root: &std::path::Path, rel: &str) -> String {
    fs::read_to_string(path_root.join(rel)).unwrap_or_else(|e| panic!("read {rel}: {e}"))
}

// --- Two-way split across kinds ------------------------------------------

#[test]
fn const_two_siblings_split_to_two_modules() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1, b = 2;
console.log(a, b);
export { a, b };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));

    let mod_a = read(&fixture.out_root, "static/app/modules/mod_a.js");
    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");

    assert!(
        mod_a.contains("const a = 1"),
        "mod_a.js must declare `const a = 1`; got:\n{mod_a}",
    );
    assert!(
        !mod_a.contains("b = 2"),
        "mod_a.js must not steal `b = 2`; got:\n{mod_a}",
    );
    assert!(
        mod_b.contains("const b = 2"),
        "mod_b.js must declare `const b = 2`; got:\n{mod_b}",
    );
    assert!(
        !mod_b.contains("a = 1"),
        "mod_b.js must not steal `a = 1`; got:\n{mod_b}",
    );

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_a.js",
        &["a"],
        &["b"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &["b"],
        &["a"],
    );
    assert_entry_output(&fixture, "1 2\n");
}

#[test]
fn let_two_siblings_split_to_two_modules() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let a = 1, b = 2;
console.log(a, b);
export { a, b };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));

    let mod_a = read(&fixture.out_root, "static/app/modules/mod_a.js");
    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");

    assert!(
        mod_a.contains("let a = 1"),
        "mod_a.js must declare `let a = 1` (preserving `let`); got:\n{mod_a}",
    );
    assert!(
        mod_b.contains("let b = 2"),
        "mod_b.js must declare `let b = 2` (preserving `let`); got:\n{mod_b}",
    );
    assert_entry_output(&fixture, "1 2\n");
}

#[test]
fn var_two_siblings_split_to_two_modules() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var a = 1, b = 2;
console.log(a, b);
export { a, b };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));

    let mod_a = read(&fixture.out_root, "static/app/modules/mod_a.js");
    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");

    assert!(
        mod_a.contains("var a = 1"),
        "mod_a.js must declare `var a = 1` (preserving `var`); got:\n{mod_a}",
    );
    assert!(
        mod_b.contains("var b = 2"),
        "mod_b.js must declare `var b = 2` (preserving `var`); got:\n{mod_b}",
    );
    assert_entry_output(&fixture, "1 2\n");
}

// --- export const comma-list ---------------------------------------------

#[test]
fn export_const_two_siblings_split_to_two_modules() {
    // `export const a = 1, b = 2;` — both bindings exported.
    // After split each module must own the right binding and
    // expose it as an export.
    let fixture = run_fixture(FixtureOpts::new(
        r#"export const a = 1, b = 2;
console.log(a, b);
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_a.js",
        &["a"],
        &["b"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &["b"],
        &["a"],
    );

    let mod_a = read(&fixture.out_root, "static/app/modules/mod_a.js");
    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");
    assert!(
        mod_a.contains("a = 1") && !mod_a.contains("b = 2"),
        "mod_a.js must own only `a = 1`; got:\n{mod_a}",
    );
    assert!(
        mod_b.contains("b = 2") && !mod_b.contains("a = 1"),
        "mod_b.js must own only `b = 2`; got:\n{mod_b}",
    );
    assert_entry_output(&fixture, "1 2\n");
}

// --- Three-way split ------------------------------------------------------

#[test]
fn three_siblings_split_to_three_modules() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1, b = 2, c = 3;
console.log(a, b, c);
export { a, b, c };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_b", &[Member::new("b")]),
            logical_module("mod_c", &[Member::new("c")]),
        ],
    ));

    let mod_a = read(&fixture.out_root, "static/app/modules/mod_a.js");
    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");
    let mod_c = read(&fixture.out_root, "static/app/modules/mod_c.js");
    assert!(
        mod_a.contains("const a = 1") && !mod_a.contains("b = 2") && !mod_a.contains("c = 3"),
        "mod_a.js must own only `a = 1`; got:\n{mod_a}",
    );
    assert!(
        mod_b.contains("const b = 2") && !mod_b.contains("a = 1") && !mod_b.contains("c = 3"),
        "mod_b.js must own only `b = 2`; got:\n{mod_b}",
    );
    assert!(
        mod_c.contains("const c = 3") && !mod_c.contains("a = 1") && !mod_c.contains("b = 2"),
        "mod_c.js must own only `c = 3`; got:\n{mod_c}",
    );
    assert_entry_output(&fixture, "1 2 3\n");
}

// --- Partial claim leaves the rest in the residual comma-list ------------

#[test]
fn partial_claim_leaves_unclaimed_in_residual() {
    // mod_x claims `a` and `c`; `b` is unclaimed and must remain
    // in residual (as part of a 1-decl `const b = 2;` — the split
    // drops the comma-list).
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1, b = 2, c = 3;
console.log(a, b, c);
export { a, b, c };
"#,
        vec![logical_module(
            "mod_x",
            &[Member::new("a"), Member::new("c")],
        )],
    ));

    let mod_x = read(&fixture.out_root, "static/app/modules/mod_x.js");
    let residual = read(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
    );

    assert!(
        mod_x.contains("a = 1") && mod_x.contains("c = 3") && !mod_x.contains("b = 2"),
        "mod_x.js must own `a` and `c`, not `b`; got:\n{mod_x}",
    );
    assert!(
        residual.contains("b = 2") && !residual.contains("a = 1") && !residual.contains("c = 3"),
        "residual must own only `b`; got:\n{residual}",
    );
    assert_entry_output(&fixture, "1 2 3\n");
}

// --- Side-effecting sibling initializer ----------------------------------

#[test]
fn mixed_purity_sibling_init_preserves_side_effect_module() {
    // `const A = 1, B = sideEffect();` — `B`'s init is impure.
    // Splitting must keep `B`'s init evaluation in `mod_b`. The
    // entry imports both modules so `B`'s init runs.
    let fixture = run_fixture(FixtureOpts::new(
        r#"function sideEffect() { console.log("init"); return 42; }
const A = 1, B = sideEffect();
console.log(A, B);
export { A, B };
"#,
        vec![
            logical_module("mod_a", &[Member::new("A")]),
            logical_module("mod_b", &[Member::new("B")]),
        ],
    ));

    let mod_a = read(&fixture.out_root, "static/app/modules/mod_a.js");
    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");
    assert!(
        mod_a.contains("const A = 1") && !mod_a.contains("sideEffect()"),
        "mod_a.js must not contain `sideEffect()` call; got:\n{mod_a}",
    );
    assert!(
        mod_b.contains("sideEffect()"),
        "mod_b.js must keep the `sideEffect()` call; got:\n{mod_b}",
    );

    // Behaviour: prints `init` once during module init, then `1 42`.
    assert_entry_output(&fixture, "init\n1 42\n");
}

// --- Cross-sibling read (eager) ------------------------------------------

#[test]
fn sibling_reads_other_sibling_eagerly() {
    // `const a = 1, b = a + 1;` — `b`'s RHS reads `a`. When the
    // two land in separate modules, `mod_b` must import `a` from
    // `mod_a` (cross-module eager read).
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1, b = a + 1;
console.log(a, b);
export { a, b };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));

    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");
    assert!(
        mod_b.contains("import"),
        "mod_b.js must import `a` from mod_a; got:\n{mod_b}",
    );
    assert!(
        mod_b.contains("const b = a + 1"),
        "mod_b.js must keep `b = a + 1`; got:\n{mod_b}",
    );
    assert_entry_output(&fixture, "1 2\n");
}

// --- Cross-sibling read (lazy: function body) ----------------------------

#[test]
fn sibling_reads_other_sibling_lazily() {
    // `const a = 1, b = () => a + 1;` — `b`'s read is inside a
    // function body (lazy). The split must still produce a working
    // import.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1, b = () => a + 1;
console.log(a, b());
export { a, b };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));

    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");
    assert!(
        mod_b.contains("import"),
        "mod_b.js must import `a` from mod_a; got:\n{mod_b}",
    );
    assert!(
        mod_b.contains("=> a + 1") || mod_b.contains("=>a + 1"),
        "mod_b.js must keep `() => a + 1`; got:\n{mod_b}",
    );
    assert_entry_output(&fixture, "1 2\n");
}

// --- Cross-comma-list flow with two comma-lists -------------------------

#[test]
fn two_comma_lists_split_independently() {
    // Two separate comma-lists. Each declarator is claimed by a
    // distinct module. Per-declarator split must not pull both
    // comma-list halves to one module just because they share a
    // statement.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1, b = 2;
const c = 3, d = 4;
console.log(a, b, c, d);
export { a, b, c, d };
"#,
        vec![
            logical_module("mod_ad", &[Member::new("a"), Member::new("d")]),
            logical_module("mod_bc", &[Member::new("b"), Member::new("c")]),
        ],
    ));

    let mod_ad = read(&fixture.out_root, "static/app/modules/mod_ad.js");
    let mod_bc = read(&fixture.out_root, "static/app/modules/mod_bc.js");
    assert!(
        mod_ad.contains("a = 1")
            && mod_ad.contains("d = 4")
            && !mod_ad.contains("b = 2")
            && !mod_ad.contains("c = 3"),
        "mod_ad.js must own only `a` and `d`; got:\n{mod_ad}",
    );
    assert!(
        mod_bc.contains("b = 2")
            && mod_bc.contains("c = 3")
            && !mod_bc.contains("a = 1")
            && !mod_bc.contains("d = 4"),
        "mod_bc.js must own only `b` and `c`; got:\n{mod_bc}",
    );
    assert_entry_output(&fixture, "1 2 3 4\n");
}

// --- Destructuring patterns ----------------------------------------------

#[test]
fn destructure_only_declarator_moves_with_first_name() {
    // A single declarator with a destructuring pattern binds
    // multiple names. The split treats the whole declarator as
    // atomic: claiming any one of the names moves the entire
    // declarator (and all names) to that module. Verify the
    // behaviour explicitly.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const obj = { x: 10, y: 20 };
const { x, y } = obj;
console.log(x, y);
export { x, y };
"#,
        vec![logical_module("mod_xy", &[Member::new("x")])],
    ));

    let mod_xy = read(&fixture.out_root, "static/app/modules/mod_xy.js");
    // The destructuring declarator either moves atomically (both
    // `x` and `y` end up in mod_xy) or only `x` does and `y` lands
    // somewhere reachable; whichever shape the materializer
    // picks, the program must still print "10 20".
    assert!(
        mod_xy.contains("{ x, y }") || mod_xy.contains("{x, y}") || mod_xy.contains("{x,y}"),
        "mod_xy.js must keep the destructuring declarator intact; got:\n{mod_xy}",
    );
    assert_entry_output(&fixture, "10 20\n");
}

#[test]
fn destructure_split_across_modules_is_rejected() {
    // `const { x, y } = obj;` — `x` claimed by mod_x, `y` claimed
    // by mod_y. Destructure declarators move atomically, so the
    // materializer must reject this spec rather than emit a
    // broken split.
    let opts = FixtureOpts::new(
        r#"const obj = { x: 10, y: 20 };
const { x, y } = obj;
console.log(x, y);
export { x, y };
"#,
        vec![
            logical_module("mod_x", &[Member::new("x")]),
            logical_module("mod_y", &[Member::new("y")]),
        ],
    );
    expect_rejection_containing_all(
        opts,
        &[
            "destructure",
            "mod_x",
            "mod_y",
            // Both names in the offending pattern surface in the
            // error so the spec author can find the conflicting
            // pair without re-reading source.
            "x",
            "y",
        ],
    );
}

#[test]
fn comma_list_with_destructuring_sibling_splits() {
    // `const a = 1, { x, y } = obj, b = 2;` — three declarators
    // sharing one `const`. Each can go to a different module;
    // the destructuring one moves atomically.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const obj = { x: 10, y: 20 };
const a = 1, { x, y } = obj, b = 2;
console.log(a, x, y, b);
export { a, x, y, b };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_xy", &[Member::new("x")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));

    let mod_a = read(&fixture.out_root, "static/app/modules/mod_a.js");
    let mod_xy = read(&fixture.out_root, "static/app/modules/mod_xy.js");
    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");
    assert!(
        mod_a.contains("a = 1") && !mod_a.contains("{ x, y }"),
        "mod_a.js must own only `a = 1`; got:\n{mod_a}",
    );
    assert!(
        mod_xy.contains("x") && mod_xy.contains("y"),
        "mod_xy.js must own the destructuring declarator; got:\n{mod_xy}",
    );
    assert!(
        mod_b.contains("b = 2") && !mod_b.contains("{ x, y }"),
        "mod_b.js must own only `b = 2`; got:\n{mod_b}",
    );
    assert_entry_output(&fixture, "1 10 20 2\n");
}

// --- Comma-list with side-effecting prior init affecting later sibling ---

#[test]
fn comma_list_evaluates_in_source_order_after_split() {
    // `const a = touch("a"), b = touch("b");` — split into two
    // modules. The ESM linker must run the two modules in source
    // order so the prints come out "a" then "b".
    let fixture = run_fixture(FixtureOpts::new(
        r#"const log = [];
function touch(t) { log.push(t); return t; }
const a = touch("a"), b = touch("b");
console.log(log.join(","));
console.log(a, b);
export { a, b };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));

    let mod_a = read(&fixture.out_root, "static/app/modules/mod_a.js");
    let mod_b = read(&fixture.out_root, "static/app/modules/mod_b.js");
    assert!(
        mod_a.contains("touch(\"a\")"),
        "mod_a.js must keep `touch(\"a\")`; got:\n{mod_a}",
    );
    assert!(
        mod_b.contains("touch(\"b\")"),
        "mod_b.js must keep `touch(\"b\")`; got:\n{mod_b}",
    );

    // The two modules must initialize in source order — `a` first,
    // `b` second.
    assert_entry_output(&fixture, "a,b\na b\n");
}
