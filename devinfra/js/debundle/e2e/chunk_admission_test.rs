//! Input-chunk admission checks (docs/design.md A1/A3/A5): chunks
//! containing shapes outside the realizability theorem's input subset
//! are rejected before any quotient or lowering work, with the
//! offending statement ordinal in the diagnostic; audited chunks can
//! opt out per check via
//! `chunk_analysis_options.<chunk>.admission_overrides`. A4 (`with`)
//! is rejected earlier, at parse (module code is strict) — pinned
//! here too. The A2 (top-level await) sibling bail is pinned in
//! `realizability_test.rs::top_level_await_is_rejected`.

use debundle_e2e_support::{
    FixtureOpts, Member, assert_entry_output, expect_rejection_containing_all, logical_module,
    run_fixture,
};

// --- A1: no `eval` at module top level -----------------------------------

#[test]
fn top_level_direct_eval_is_rejected() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const A = 1;
eval("globalThis.tag = 1");
console.log(A);
export { A };
"#,
            vec![logical_module("mod_x", &[Member::new("A")])],
        ),
        // Diagnostic contract: the assumption tag, the offending
        // statement ordinal, and the override spelling.
        &["a1_eval", "statement #1", "eval", "admission_overrides"],
    );
}

#[test]
fn top_level_indirect_seq_eval_is_rejected() {
    // `(0, eval)(...)` — the callee hides behind a comma sequence;
    // the admission scan must look through parens AND SeqExpr.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const A = 1;
(0, eval)("globalThis.tag = 1");
export { A };
"#,
            vec![logical_module("mod_x", &[Member::new("A")])],
        ),
        &["a1_eval", "statement #1", "eval"],
    );
}

#[test]
fn eval_inside_function_body_is_allowed() {
    // A1 only bans module-top eval. A lazy (function-body) eval is a
    // documented residual risk, not an admission violation — see
    // docs/design.md §"Coverage gaps".
    let fixture = run_fixture(FixtureOpts::new(
        r#"function reader() {
  return eval("1 + 1");
}
const A = "ok";
console.log(A);
export { A, reader };
"#,
        vec![logical_module("mod_x", &[Member::new("A")])],
    ));
    assert_entry_output(&fixture, "ok\n");
}

// --- A3: no dynamic import of debundled internal modules -----------------

#[test]
fn dynamic_import_of_own_chunk_is_rejected() {
    // The literal specifier resolves back into the chunk being
    // debundled — an internal dynamic import that routes around the
    // static import graph `I`.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const A = 1;
const again = () => import("./app.js");
export { A, again };
"#,
            vec![logical_module("mod_x", &[Member::new("A")])],
        ),
        &[
            "a3_dynamic_import",
            "statement #1",
            "resolves into this chunk",
        ],
    );
}

#[test]
fn top_level_non_literal_dynamic_import_is_rejected() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const name = "./extra.js";
import(name);
const A = 1;
export { A };
"#,
            vec![logical_module("mod_x", &[Member::new("A")])],
        ),
        &["a3_dynamic_import", "statement #1", "non-literal"],
    );
}

#[test]
fn external_and_lazy_dynamic_imports_are_allowed() {
    // External literal targets are black-box leaves (allowed even at
    // top level); non-literal specifiers are allowed in lazy
    // positions (documented residual gap).
    let fixture = run_fixture(FixtureOpts::new(
        r#"const lazyByName = (name) => import(name);
const loadVendor = () => import("some-external-package");
const A = "ok";
console.log(A);
export { A, lazyByName, loadVendor };
"#,
        vec![logical_module("mod_x", &[Member::new("A")])],
    ));
    assert_entry_output(&fixture, "ok\n");
}

// --- A4: no `with` blocks -------------------------------------------------

#[test]
fn with_block_is_rejected_at_parse() {
    // Module code is strict per ECMA-262, and the parser surfaces
    // `with` as a recoverable parse error that fails chunk loading —
    // so A4 never reaches the chunk-analysis admission scan and needs no AST
    // check there. This test pins the parse-time rejection (and that
    // the error message names the strict-mode `with` ban, not just an
    // opaque error count).
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const obj = { x: 1 };
function f() {
  with (obj) {
    return x;
  }
}
const A = 1;
export { A, f };
"#,
            vec![logical_module("mod_x", &[Member::new("A")])],
        ),
        &["failed to parse", "with"],
    );
}

// --- A5: no namespace reflection beyond `import.meta.url` ----------------

#[test]
fn top_level_import_meta_reflection_is_rejected() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const env = import.meta.env;
const A = 1;
export { A, env };
"#,
            vec![logical_module("mod_x", &[Member::new("A")])],
        ),
        &["a5_import_meta", "statement #0", "import.meta.env"],
    );
}

#[test]
fn import_meta_url_is_allowed() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const here = import.meta.url;
const A = "ok";
console.log(A);
export { A, here };
"#,
        vec![logical_module("mod_x", &[Member::new("A")])],
    ));
    assert_entry_output(&fixture, "ok\n");
}

// --- Spec-level override escape hatch -------------------------------------

#[test]
fn admission_override_admits_audited_chunk_with_notice() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const A = "ok";
eval("1 + 1");
console.log(A);
export { A };
"#,
            vec![logical_module("mod_x", &[Member::new("A")])],
        )
        .with_admission_overrides(&["a1_eval"]),
    );
    // Every override use prints a one-line notice naming the check
    // and the suppressed violation.
    assert!(
        fixture
            .stderr
            .contains("admission check a1_eval overridden by spec"),
        "stderr missing override notice:\n{}",
        fixture.stderr,
    );
    assert_entry_output(&fixture, "ok\n");
}

#[test]
fn redundant_admission_override_warns() {
    // An override that no longer suppresses anything is stale trust
    // surface — surfaced like redundant purity hints.
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const A = "ok";
console.log(A);
export { A };
"#,
            vec![logical_module("mod_x", &[Member::new("A")])],
        )
        .with_admission_overrides(&["a5_import_meta"]),
    );
    assert!(
        fixture
            .stderr
            .contains("admission override `a5_import_meta` is redundant"),
        "stderr missing redundant-override warning:\n{}",
        fixture.stderr,
    );
}
