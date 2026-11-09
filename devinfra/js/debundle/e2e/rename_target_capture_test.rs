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

const UNBOUND_TYPEOF_CAPTURE_SOURCE: &str = r#"const source_value = "A";
function read() {
  return source_value + typeof unbound_value;
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

/// `chunk_renames` path: the rename applies to entry's body, where a
/// nested function binds the target name and reads the source binding.
#[test]
fn chunk_rename_target_captured_by_nested_binding_is_rejected() {
    let opts = FixtureOpts::new(
        r#"var source_value = "A";
function f(taken_name) {
  return source_value + taken_name;
}
console.log(f("B"));
export { source_value, f };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_rename("taken_name", "source_value"))
    .with_unassigned_mode(unassigned_mode_inline());
    expect_rejection_containing_all(opts, &["source_value", "taken_name"]);
}

/// Plan-driven naturalization path: a logical-module member rename
/// (`a` exported as `b`) applies module-wide to the moved body, where
/// `f`'s parameter `b` would capture the renamed reads of `a`.
#[test]
fn module_member_rename_target_captured_by_nested_binding_is_rejected() {
    let opts = FixtureOpts::new(
        r#"var source_value = "A";
function f(taken_name) {
  return source_value + taken_name;
}
console.log(f("B"));
export { source_value, f };
"#,
        vec![logical_module(
            "x",
            &[
                Member::renamed("taken_name", "source_value"),
                Member::new("f"),
            ],
        )],
    );
    expect_rejection_containing_all(opts, &["source_value", "taken_name"]);
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

/// A moved logical-module member rename must not turn an unbound identifier
/// read by `typeof` into a reference to the renamed top-level binding.
#[test]
fn module_member_rename_cannot_capture_unbound_typeof_name() {
    let baseline = run_fixture(
        FixtureOpts::new(UNBOUND_TYPEOF_CAPTURE_SOURCE, vec![])
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&baseline, "Aundefined\n");

    let opts = FixtureOpts::new(
        UNBOUND_TYPEOF_CAPTURE_SOURCE,
        vec![logical_module(
            "values",
            &[
                Member::renamed("unbound_value", "source_value"),
                Member::new("read"),
            ],
        )],
    );
    expect_rename_refusal_with_candidate_output(
        opts,
        "Aundefined\n",
        "Astring\n",
        &["source_value", "unbound_value"],
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
    let baseline = run_fixture(FixtureOpts::new(SOURCE, vec![]));
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
    );
    expect_rename_refusal_with_candidate_output(
        opts,
        "undefined\n",
        "string\n",
        &["source_value", "unbound_value"],
    );
}

/// An ident with the target spelling can already resolve to a hoisted local.
/// Its exact resolver context keeps it outside reverse-capture refusal even
/// when the existing name-only scope walk does not model `var` hoisting.
#[test]
fn chunk_rename_leaves_hoisted_inner_target_reference_local() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const source_value = "A";
function read() {
  const before = typeof taken_name;
  if (false) {
    var taken_name;
  }
  return before;
}
console.log(source_value + read());
export { source_value, read };
"#,
            vec![],
        )
        .with_chunk_renames(chunk_rename("taken_name", "source_value"))
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "Aundefined\n");
}

/// A shadowing parameter alone is safe when no renamed-source reference occurs
/// in its scope; the top-level source reference must still resolve to the rename.
#[test]
fn chunk_rename_target_shadowed_without_source_read_stays_local() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const source_value = "A";
function read(taken_name) {
  return taken_name;
}
console.log(source_value + read("B"));
export { source_value, read };
"#,
            vec![],
        )
        .with_chunk_renames(chunk_rename("taken_name", "source_value"))
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "AB\n");
}

/// Sibling collision: the rename target is another top-level binding
/// of the same module body (a destructure sibling pulled along with
/// the claimed binding). Renaming `origin_name` -> `sibling_name` would
/// declare `sibling_name` twice in one pattern.
#[test]
fn module_member_rename_target_colliding_with_sibling_binding_is_rejected() {
    let opts = FixtureOpts::new(
        r#"const { origin_name, sibling_name } = { origin_name: "A", sibling_name: "R" };
console.log(origin_name + sibling_name);
export { origin_name, sibling_name };
"#,
        vec![logical_module(
            "x",
            &[Member::renamed("sibling_name", "origin_name")],
        )],
    );
    expect_rejection_containing_all(opts, &["origin_name", "sibling_name"]);
}
