use super::case::{Expectation, Rejected};
use super::{Case, InputFile, cases};
use debundle_e2e_support::{
    CaseWorkspace, FixtureOpts, assert_module_exports, assert_node_output,
    assert_preserved_entry_output, failed_fixture_dirs_for_current_test, process,
    recorded_commands, replay_command, run_fixture, unassigned_mode_inline, write_text_file,
    write_yaml_file,
};
use std::panic::catch_unwind;

fn named_case(name: &str) -> Case {
    cases()
        .into_iter()
        .find(|case| case.name() == name)
        .expect("corpus example")
}

#[test]
fn wrong_stdout_does_not_pass() {
    let mut case = named_case("emission/node_builtin_import/spec");
    let Expectation::Accept { accept } = &mut case.expectation else {
        unreachable!()
    };
    accept.stdout = "wrong output\n".to_owned();
    assert!(catch_unwind(|| case.run()).is_err());
}

#[test]
fn discovers_and_executes_a_second_analyzed_chunk_without_a_list() {
    let inputs = CaseWorkspace::with_retention_root(None);
    let main = inputs.path().join("main.js");
    let peer = inputs.path().join("peer.js");
    write_text_file(
        &main,
        "import { value } from './peer.js';\nconst impl = value;\nfunction show() { return impl; }\nconsole.log(show());\nexport { show };\n",
    );
    write_text_file(&peer, "export const value = 'impl-value';\n");
    let mut case = named_case("emission/reserved_import_name/spec");
    case.inputs
        .iter_mut()
        .find(|file| file.relative == std::path::Path::new("main.js"))
        .unwrap()
        .source = main;
    case.inputs.push(InputFile {
        relative: "peer.js".into(),
        source: peer,
    });
    case.run();
}

#[test]
fn explicit_spec_paths_override_corpus_defaults() {
    let inputs = CaseWorkspace::with_retention_root(None);
    let mut case = named_case("emission/export_local_scope/spec");
    let spec = case
        .inputs
        .iter_mut()
        .find(|file| file.relative == case.spec)
        .unwrap();
    let mut authored: serde_yaml::Mapping =
        serde_yaml::from_slice(&std::fs::read(&spec.source).unwrap()).unwrap();
    let overrides: serde_yaml::Mapping = serde_yaml::from_str(
        "inputs:\n  input_root: source\n  js_list_path: chosen-list.txt\n\
         materialize_logical_modules:\n  target_dir: moved\n  report_out_dir: custom-reports\n\
         write_js_tree:\n  out_dir: custom-output\n",
    )
    .unwrap();
    authored.extend(overrides);
    spec.source = inputs.path().join("spec.yaml");
    write_yaml_file(&spec.source, &authored);
    case.inputs
        .iter_mut()
        .find(|file| file.relative == std::path::Path::new("main.js"))
        .unwrap()
        .relative = "source/main.js".into();
    let listing = inputs.path().join("chosen-list.txt");
    write_text_file(&listing, "main.js\n");
    case.inputs.push(InputFile {
        relative: "chosen-list.txt".into(),
        source: listing,
    });
    let Expectation::Accept { accept } = &mut case.expectation else {
        unreachable!()
    };
    let exports = accept
        .exports
        .remove(std::path::Path::new("main/modules/x.js"))
        .unwrap();
    accept.exports.insert("main/moved/x.js".into(), exports);
    case.run();
}

#[test]
fn wrong_conflict_cause_does_not_pass() {
    let mut case = named_case("initialization/nested_closure_rebind/separated.spec");
    let Expectation::Reject { reject } = &mut case.expectation else {
        unreachable!()
    };
    let Rejected::AtomicConflict {
        atomic_conflict, ..
    } = reject
    else {
        unreachable!()
    };
    atomic_conflict.causes = vec!["missing_cause".to_owned()];
    assert!(catch_unwind(|| case.run()).is_err());
}

#[test]
fn export_probe_handles_partial_and_deferred_stdout() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"process.stdout.write("prefix");
console.log("line");
setTimeout(() => process.stdout.write("suffix"), 0);
export const value = 7;
"#,
            vec![],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "prefixline\nsuffix");
    assert_module_exports(
        &fixture.out_root,
        "static/app/entry.js",
        &["value"],
        &["missing"],
    );
}

#[test]
fn post_run_assertion_failure_retains_replayable_inputs() {
    let mut case = named_case("emission/rename_shorthand/chunk.spec");
    let Expectation::Accept { accept } = &mut case.expectation else {
        unreachable!()
    };
    accept
        .exports
        .values_mut()
        .next()
        .unwrap()
        .include
        .push("missing_export".to_owned());
    let failure = catch_unwind(|| case.run()).expect_err("missing export must fail");
    assert!(
        failure
            .downcast_ref::<String>()
            .is_some_and(|message| message.contains("to export missing_export")),
        "the export assertion must fail after successfully reading the logged entry's exports"
    );

    let retained = failed_fixture_dirs_for_current_test();
    let [retained] = retained.as_slice() else {
        panic!("expected one retained corpus case: {retained:?}")
    };
    let command = recorded_commands(retained)
        .unwrap()
        .into_iter()
        .find(|command| command.debundle)
        .unwrap();
    assert!(
        command.snapshot_errors.is_empty(),
        "{:?}",
        command.snapshot_errors
    );
    let workspace = CaseWorkspace::with_retention_root(None);
    let destination = workspace.path().join("replayed");
    let replayed =
        replay_command(retained, &command.id, &destination, &command.program, false).unwrap();
    process::assert_success(&replayed);
    let entry = destination
        .join(command.cwd.file_name().unwrap())
        .join("out/app/main/entry.js");
    assert_node_output(&entry, "AAA\n", "");
}

#[test]
fn syntax_check_catches_an_unimported_runtime_sibling() {
    let inputs = CaseWorkspace::with_retention_root(None);
    let broken = inputs.path().join("broken.fixture");
    write_text_file(&broken, "export const = ;\n");
    let mut case = named_case("emission/node_builtin_import/spec");
    case.inputs.push(InputFile {
        relative: "broken.fixture".into(),
        source: broken,
    });
    let Expectation::Accept { accept } = &mut case.expectation else {
        unreachable!()
    };
    accept
        .runtime_files
        .insert("main/unused.js".into(), "broken.fixture".into());
    // The entry does not import this sibling, so ordinary runtime checks pass.
    case.run();
    let Expectation::Accept { accept } = &mut case.expectation else {
        unreachable!()
    };
    accept.check_all_js = true;
    let failure =
        catch_unwind(|| case.run()).expect_err("invalid unused sibling must fail syntax checking");
    assert!(
        failure
            .downcast_ref::<String>()
            .is_some_and(|message| message.contains("node --check failed"))
    );
}

fn written_case(source: &str, spec: &str, expectation: &str) -> (CaseWorkspace, Case) {
    let inputs = CaseWorkspace::with_retention_root(None);
    let mut files = Vec::new();
    for (name, text) in [("main.js", source), ("spec.yaml", spec)] {
        let path = inputs.path().join(name);
        write_text_file(&path, text);
        files.push(InputFile {
            relative: name.into(),
            source: path,
        });
    }
    let case = Case {
        name: "runner_check".into(),
        spec: "spec.yaml".into(),
        inputs: files,
        expectation: serde_yaml::from_str(expectation).unwrap(),
    };
    (inputs, case)
}

#[test]
fn cycle_refusal_checks_modules_and_sequenced_cut() {
    let (_inputs, mut case) = written_case(
        "const a = (() => 1)();\nconst b = Object.entries((() => ({ x: 1 }))());\nconst c = b.length + a;\nconsole.log(c);\nexport { a, b, c };\n",
        "logical_modules:\n  main:\n    b_module:\n      members: [{selector: {binding: {name: b}}}]\nunassigned_mode:\n  main: {kind: catchall_file}\nimport_order:\n  main: [b_module, residual]\n",
        "reject:\n  cycle:\n    modules: [b_module, residual]\n    sequenced: true\n",
    );
    case.run();
    let Expectation::Reject {
        reject: Rejected::Cycle { cycle, .. },
    } = &mut case.expectation
    else {
        unreachable!()
    };
    cycle.modules.push("absent_module".into());
    let failure = catch_unwind(|| case.run()).expect_err("wrong cycle module must fail");
    assert!(
        failure
            .downcast_ref::<String>()
            .is_some_and(|message| message.contains("no blocking SCC"))
    );
}

#[test]
fn error_refusal_checks_every_expected_token() {
    let (_inputs, mut case) = written_case(
        "function pick() { return 'first'; }\nfunction pick() { return 'second'; }\nconsole.log(pick());\nexport { pick };\n",
        "logical_modules:\n  main:\n    selected:\n      members: [{selector: {binding: {name: pick}}}]\nunassigned_mode:\n  main: {kind: catchall_file}\nimport_order:\n  main: [selected]\n",
        "reject:\n  stderr_contains_all: [pick]\n",
    );
    case.run();
    let Expectation::Reject {
        reject: Rejected::Stderr {
            stderr_contains_all,
            ..
        },
    } = &mut case.expectation
    else {
        unreachable!()
    };
    stderr_contains_all.push("absent_error_token".into());
    let failure = catch_unwind(|| case.run()).expect_err("wrong refusal token must fail");
    assert!(
        failure
            .downcast_ref::<String>()
            .is_some_and(|message| message.contains("stderr missing"))
    );
}

#[test]
fn rejection_reason_is_exclusive_and_rejects_unknown_fields() {
    for yaml in [
        "reject: {cycle: {modules: [a]}, stderr_contains_all: [a]}",
        "reject: {cycle: {modules: [a], unexpected: true}}",
        "reject: {stderr_contains_all: [a], unexpected: true}",
    ] {
        assert!(serde_yaml::from_str::<Expectation>(yaml).is_err(), "{yaml}");
    }
}

#[test]
fn explicit_original_output_still_checks_both_entries() {
    let (_inputs, mut case) = written_case(
        "function a() {} console.log(a.name); export { a };\n",
        "logical_modules:\n  main:\n    renamed:\n      members: [{name: readable, selector: {binding: {name: a}}}]\nunassigned_mode:\n  main: {kind: catchall_file}\nimport_order:\n  main: [renamed]\n",
        r#"accept:
  stdout: "readable\n"
  original_stdout: "a\n"
"#,
    );
    case.run();
    let Expectation::Accept { accept } = &mut case.expectation else {
        unreachable!()
    };
    accept.original_stdout = Some("wrong original\n".into());
    assert!(catch_unwind(|| case.run()).is_err());
    let Expectation::Accept { accept } = &mut case.expectation else {
        unreachable!()
    };
    accept.original_stdout = Some("a\n".into());
    accept.stdout = "wrong emitted\n".into();
    assert!(catch_unwind(|| case.run()).is_err());
}
