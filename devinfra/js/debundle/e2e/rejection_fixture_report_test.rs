//! Missing conflict reports must retain the earlier CLI rejection's context.

use debundle_e2e_support::*;
use std::panic::{AssertUnwindSafe, catch_unwind};

#[test]
fn missing_conflict_report_includes_case_path_and_cli_stderr() {
    let fixture = run_rejection_fixture(mixed_selector_failure_fixture());
    assert!(!fixture.stderr.is_empty());
    for filename in ["cycles.json", "atomic_group_conflicts.json"] {
        let path = fixture.report_root.join(&fixture.chunk_id).join(filename);
        assert!(
            !path.exists(),
            "selector rejection must precede conflict reports"
        );
        let panic = catch_unwind(|| match filename {
            "cycles.json" => fixture.cycles(),
            _ => fixture.atomic_group_conflicts(),
        })
        .expect_err("reading the missing report must fail");
        let message = panic.downcast_ref::<String>().expect("diagnostic string");
        assert!(message.contains(path.to_str().unwrap()), "{message}");
        assert!(message.contains(&fixture.stderr), "{message}");
    }
}

#[test]
fn unexpected_exit_preserves_cross_chunk_outputs_for_node_inspection() {
    let mut opts = FixtureOpts::new(
        r#"import { h as x } from "./provider.js";
console.log(x());
const guard = 0;
export { x, guard };
"#,
        vec![logical_module("guard", &[Member::new("guard")])],
    )
    .with_chunk_id("static/importer");
    opts.unassigned_mode = unassigned_mode_inline();
    opts.extra_chunks = &[(
        "static/provider",
        "function p() { return 7; }\nexport { p as h };\n",
    )];
    let provider_modules = [(
        "static/provider",
        vec![logical_module(
            "provider",
            &[Member::renamed("readable", "p")],
        )],
    )];
    opts.extra_chunk_logical_modules = &provider_modules;

    let panic = match catch_unwind(AssertUnwindSafe(move || {
        run_rejection_fixture_with_args(opts, &[], 3)
    })) {
        Ok(_) => panic!("the accepted fixture must disagree with the deliberately wrong exit code"),
        Err(panic) => panic,
    };
    let message = panic
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| panic.downcast_ref::<&str>().copied())
        .expect("wrong-exit assertion panic message");
    assert!(
        message.contains("expected spec to be rejected"),
        "{message}"
    );
    assert!(message.contains("left: Some(0)"), "{message}");
    assert!(message.contains("right: Some(3)"), "{message}");

    let directories = failed_fixture_dirs_for_current_test();
    let [preserved] = directories.as_slice() else {
        panic!("expected one retained failure directory, got {directories:#?}");
    };
    let app = preserved.join("app");
    let program_graph = preserved.join("program_graph.json.gz");
    let importer_entry = app.join("static/importer/entry.js");
    let provider_entry = app.join("static/provider/entry.js");
    let package_json = app.join("package.json");
    let provider_module = app.join("static/provider/modules/provider.js");
    for path in [
        &program_graph,
        &package_json,
        &importer_entry,
        &provider_entry,
        &provider_module,
    ] {
        assert!(
            path.is_file(),
            "missing preserved artifact {}",
            path.display()
        );
    }

    assert_node_output(&importer_entry, "7\n", "");
}
