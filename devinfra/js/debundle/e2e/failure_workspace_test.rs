//! Failure evidence follows ownership, including later runtime assertions.

use debundle_e2e_support::*;
use std::fs;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Path, PathBuf};

fn retained_case() -> PathBuf {
    let cases = failed_fixture_dirs_for_current_test();
    let [case] = cases.as_slice() else {
        panic!("expected one retained case, got {cases:?}");
    };
    let metadata = read_json::<serde_json::Value>(&case.join("case.json"));
    assert!(!Path::new(metadata["original_root"].as_str().unwrap()).exists());
    case.clone()
}

#[test]
fn runtime_mismatch_retains_sources_spec_runtime_siblings_and_emitted_tree() {
    let failure = catch_unwind(|| {
        let fixture = run_fixture(
            FixtureOpts::new("console.log(7);", vec![])
                .with_extra_files(&[("static/app/opaque.js", "export const value = 7;")]),
        );
        assert_entry_output(&fixture, "deliberately incorrect\n");
    });
    assert!(failure.is_err());
    let case = retained_case();
    for file in [
        "transform_spec.yaml",
        "snapshot/static/app.js",
        "snapshot/static/app/opaque.js",
        "out/app/static/app/opaque.js",
        "out/app/static/app/entry.js",
    ] {
        assert!(case.join(file).is_file(), "missing {file}");
    }
    assert_node_output(&case.join("out/app/static/app/entry.js"), "7\n", "");
}

#[test]
fn tree_assertion_retains_prepared_inputs_and_complete_reports() {
    assert!(
        catch_unwind(|| {
            let run = run_tree_fixture(
                &TreeFixture {
                    chunks: &[("main", "console.log(8);")],
                    module_roots: &[],
                    modules: &[],
                },
                &[],
            );
            assert_node_output(&run.out_root.join("app/main/entry.js"), "incorrect\n", "");
        })
        .is_err()
    );
    let case = retained_case();
    assert!(case.join("spec_config.yaml").is_file());
    assert!(case.join("snapshot/main.js").is_file());
    assert_node_output(&case.join("out/app/main/entry.js"), "8\n", "");
}

#[test]
fn staged_invocation_keeps_inputs_alive_after_the_original_owner_is_dropped() {
    assert!(
        catch_unwind(|| {
            let (staged, fixture) =
                saved_analysis::staged(FixtureOpts::new("console.log(9);", vec![]));
            let invocation = staged.run(&["--analysis", saved_analysis::path(&staged.artifact)]);
            drop(fixture);
            drop(staged);
            assert!(
                invocation.result.status.success(),
                "{}",
                invocation.result.stderr
            );
            assert_node_output(
                &invocation.output_root().join("app/static/app/entry.js"),
                "incorrect\n",
                "",
            );
        })
        .is_err()
    );
    let case = retained_case();
    assert!(case.join("analysis.dba").is_file());
    assert!(case.join("analysis.projection").is_file());
    assert!(case.join("snapshot/static/app.js").is_file());
    assert!(fs::read_dir(&case).unwrap().any(|entry| {
        entry
            .unwrap()
            .path()
            .join("out/app/static/app/entry.js")
            .is_file()
    }));
}

#[test]
fn simultaneous_failures_have_distinct_complete_artifacts() {
    let workspace = CaseWorkspace::with_retention_root(None);
    let outputs = workspace.path().join("retained");
    std::thread::scope(|scope| {
        for value in ["first", "second"] {
            let outputs = &outputs;
            scope.spawn(move || {
                assert!(
                    catch_unwind(|| {
                        let case = CaseWorkspace::with_retention_root(Some(outputs));
                        fs::write(case.path().join("value"), value).unwrap();
                        panic!("parallel failure");
                    })
                    .is_err()
                );
            });
        }
    });
    let mut values: Vec<_> = fs::read_dir(outputs)
        .unwrap()
        .map(|entry| fs::read_to_string(entry.unwrap().path().join("value")).unwrap())
        .collect();
    values.sort();
    assert_eq!(values, ["first", "second"]);
}

#[test]
fn retention_io_failure_preserves_the_original_panic() {
    let workspace = CaseWorkspace::with_retention_root(None);
    let blocked = workspace.path().join("not-a-directory");
    fs::write(&blocked, "occupied").unwrap();
    let panic = catch_unwind(AssertUnwindSafe(|| {
        let _case = CaseWorkspace::with_retention_root(Some(&blocked));
        panic!("original assertion");
    }))
    .expect_err("original assertion must escape");
    assert_eq!(panic.downcast_ref::<&str>(), Some(&"original assertion"));
}
