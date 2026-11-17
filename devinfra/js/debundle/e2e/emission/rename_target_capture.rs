//! Regression: a rename must never be captured by an inner binding of
//! the TARGET name. Renaming `a` -> `b` inside
//! `function f(b) { return a + b; }` would rewrite `a` to `b` and
//! silently capture the parameter (`return b + b`). Soundness over
//! completeness: the pipeline rejects such a spec with a diagnostic
//! instead of emitting a miscompiled body.

use artifact::paths::path_from_module_path;
use debundle_e2e_support::*;
use std::panic::{AssertUnwindSafe, catch_unwind};

const UNDEFINED_CAPTURE_SOURCE: &str = r#"const source_value = "A";
function read() {
  return source_value + typeof undefined;
}
console.log(read());
export { source_value, read };
"#;

fn expect_rename_refusal_with_candidate_output(
    opts: FixtureOpts<'_>,
    baseline_output: &str,
    candidate_output: &str,
    diagnostic_tokens: &[&str],
) {
    let chunk_id = opts.chunk_id.to_string();
    let result = catch_unwind(AssertUnwindSafe(|| {
        expect_rejection_containing_all(opts, diagnostic_tokens);
    }));
    if let Err(panic) = result {
        let message = panic
            .downcast_ref::<String>()
            .map(String::as_str)
            .or_else(|| panic.downcast_ref::<&str>().copied())
            .expect("unexpected-exit assertion panic message");
        assert!(
            message.contains("expected spec to be rejected"),
            "{message}"
        );
        assert!(message.contains("left: Some(0)"), "{message}");
        assert!(message.contains("right: Some(1)"), "{message}");

        let directories = failed_fixture_dirs_for_current_test();
        let [preserved] = directories.as_slice() else {
            panic!("expected one retained accepted-candidate directory, got {directories:#?}");
        };
        let entry = preserved
            .join("app")
            .join(path_from_module_path(&chunk_id))
            .join("entry.js");
        assert_node_output(&entry, candidate_output, "");
        panic!(
            "rename was accepted: baseline Node output {baseline_output:?}; candidate Node output {candidate_output:?}"
        );
    }
}

/// `chunk_renames` must not turn the global `undefined` used by `typeof`
/// into a reference to the renamed top-level binding.
#[test]
fn chunk_rename_cannot_capture_global_undefined_typeof() {
    let baseline = run_fixture(
        FixtureOpts::new(UNDEFINED_CAPTURE_SOURCE, vec![])
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&baseline, "Aundefined\n");

    let opts = FixtureOpts::new(UNDEFINED_CAPTURE_SOURCE, vec![])
        .with_chunk_renames(chunk_rename("undefined", "source_value"))
        .with_unassigned_mode(unassigned_mode_inline());
    expect_rename_refusal_with_candidate_output(
        opts,
        "Aundefined\n",
        "Astring\n",
        &["source_value", "undefined"],
    );
}

/// A source-name shadow must not hide the reverse capture: the unchanged
/// free `typeof unbound_value` reference would resolve to the introduced
/// module binding after emission.
#[test]
fn module_member_rename_cannot_capture_free_target_under_source_shadow() {
    const SOURCE: &str = r#"const source_value = "A";
function read(source_value) {
  return typeof unbound_value;
}
console.log(read("local"));
export { source_value, read };
"#;
    let baseline = run_fixture(FixtureOpts::new(SOURCE, vec![]).with_import_order(&["residual"]));
    assert_preserved_entry_output(&baseline, "undefined\n");

    let opts = FixtureOpts::new(
        SOURCE,
        vec![logical_module(
            "values",
            &[
                Member::renamed("unbound_value", "source_value"),
                Member::new("read"),
            ],
        )],
    )
    .with_import_order(&["values"]);
    expect_rename_refusal_with_candidate_output(
        opts,
        "undefined\n",
        "string\n",
        &["source_value", "unbound_value"],
    );
}
