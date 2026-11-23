//! Input-chunk admission checks (docs/design.md A1/A3/A5): chunks
//! containing shapes outside the realizability theorem's input subset
//! are rejected before any quotient or lowering work, with the
//! offending statement ordinal in the diagnostic; audited chunks can
//! opt out per check via
//! `chunk_analysis_options.<chunk>.admission_overrides`. A4 (`with`)
//! is rejected earlier, at parse (module code is strict) — pinned
//! here too. The A2 (top-level await) sibling bail is pinned in
//! `cases/realizability/top_level_await_is_rejected`.

use debundle_e2e_support::{
    FixtureOpts, Member, assert_entry_output, binding_members, logical_module, run_fixture,
};

#[test]
fn direct_eval_in_an_unsplit_chunk_is_allowed() {
    // The emitted tree is the source as one module in source order under
    // its own names, so the string code finds every binding it names.
    let source = r#"function reader() {
  return eval("A + 1");
}
const A = 1;
console.log(reader());
export { A, reader };
"#;
    for (modules, import_order) in [
        (vec![], &["residual"][..]),
        (
            vec![logical_module("mod_x", &binding_members(["reader", "A"]))],
            &["mod_x"][..],
        ),
    ] {
        let fixture =
            run_fixture(FixtureOpts::new(source, modules).with_import_order(import_order));
        assert_entry_output(&fixture, "2\n");
    }
}

#[test]
fn admission_override_notice_lists_every_suppressed_statement_once() {
    // Repeated identical violations share one notice line that lists
    // each suppressed statement, so a chunk with dozens of overridden
    // statements does not print dozens of lines.
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const A = "ok";
eval("1 + 1");
eval("2 + 2");
console.log(A);
export { A };
"#,
            vec![logical_module("mod_x", &[Member::new("A")])],
        )
        .with_admission_overrides(&["a1_eval"]),
    );
    let notices: Vec<&str> = fixture
        .stderr
        .lines()
        .filter(|line| line.contains("admission check a1_eval overridden by spec"))
        .collect();
    assert_eq!(notices.len(), 1, "stderr:\n{}", fixture.stderr);
    assert!(
        notices[0].contains("statements #1, #2: "),
        "notice does not list both statements: {}",
        notices[0],
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
