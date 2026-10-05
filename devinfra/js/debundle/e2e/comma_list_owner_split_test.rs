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

macro_rules! variable_declarator {
    ($kind:ident, $initializer:ident, [$($binding:expr),+ $(,)?]) => {
        (
            VariableDeclarationKind::$kind,
            &[$($binding),+],
            VariableInitializerKind::$initializer,
        )
    };
}

// --- Two-way split across kinds ------------------------------------------

#[test]
fn two_siblings_split_to_two_modules_keeping_the_declaration_kind() {
    for (keyword, kind) in [
        ("const", VariableDeclarationKind::Const),
        ("let", VariableDeclarationKind::Let),
        ("var", VariableDeclarationKind::Var),
    ] {
        let source = format!(
            r#"{keyword} a = 1, b = 2;
console.log(a, b);
export {{ a, b }};
"#
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![
                logical_module("mod_a", &[Member::new("a")]),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ));

        assert_module_variable_declarators(
            &fixture.out_root,
            "static/app/modules/mod_a.js",
            &[(kind, &["a"], VariableInitializerKind::Number)],
        );
        assert_module_variable_declarators(
            &fixture.out_root,
            "static/app/modules/mod_b.js",
            &[(kind, &["b"], VariableInitializerKind::Number)],
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

    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_a.js",
        &[variable_declarator!(Const, Number, ["a"])],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &[variable_declarator!(Const, Number, ["b"])],
    );
    assert_entry_output(&fixture, "1 2\n");
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

    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_x.js",
        &[
            variable_declarator!(Const, Number, ["a"]),
            variable_declarator!(Const, Number, ["c"]),
        ],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &[variable_declarator!(Const, Number, ["b"])],
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

    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_a.js",
        &[variable_declarator!(Const, Number, ["A"])],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_a.js",
        &[],
        &["sideEffect()"],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &[variable_declarator!(Const, Call, ["B"])],
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

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &["import"],
        &[],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &[variable_declarator!(Const, Binary, ["b"])],
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

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &["import"],
        &[],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &[variable_declarator!(Const, Arrow, ["b"])],
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

    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_ad.js",
        &[
            variable_declarator!(Const, Number, ["a"]),
            variable_declarator!(Const, Number, ["d"]),
        ],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_bc.js",
        &[
            variable_declarator!(Const, Number, ["b"]),
            variable_declarator!(Const, Number, ["c"]),
        ],
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

    // Claiming `x` alone pulls the whole destructure into mod_xy
    // (destructure-atomicity). The declarator must land here
    // intact with both `x` and `y` bound, even though the spec
    // only named `x`.
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_xy.js",
        &[variable_declarator!(Const, Identifier, ["x", "y"])],
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
    // The error names both claiming modules, so the spec author can find the
    // conflicting pair without re-reading source.
    expect_rejection_containing_all(opts, &["destructure", "mod_x", "mod_y"]);
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

    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_a.js",
        &[variable_declarator!(Const, Number, ["a"])],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_xy.js",
        &[variable_declarator!(Const, Identifier, ["x", "y"])],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &[variable_declarator!(Const, Number, ["b"])],
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

    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_a.js",
        &[variable_declarator!(Const, Call, ["a"])],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &[variable_declarator!(Const, Call, ["b"])],
    );

    // The two modules must initialize in source order — `a` first,
    // `b` second.
    assert_entry_output(&fixture, "a,b\na b\n");
}
