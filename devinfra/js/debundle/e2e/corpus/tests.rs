use super::case::Expectation;
use super::{Case, InputFile, cases};
use debundle_e2e_support::{
    CaseWorkspace, assert_node_output, failed_fixture_dirs_for_current_test, recorded_commands,
    replay_command, write_text_file,
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
fn wrong_conflict_cause_does_not_pass() {
    let mut case = named_case("initialization/nested_closure_rebind/separated.spec");
    let Expectation::Reject { reject } = &mut case.expectation else {
        unreachable!()
    };
    reject.atomic_conflict.causes = vec!["missing_cause".to_owned()];
    assert!(catch_unwind(|| case.run()).is_err());
}

#[test]
fn post_run_assertion_failure_retains_replayable_inputs() {
    let mut case = named_case("emission/export_local_scope/spec");
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
    assert!(catch_unwind(|| case.run()).is_err());

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
    assert!(
        replayed.status.success(),
        "{}",
        String::from_utf8_lossy(&replayed.stderr)
    );
    let entry = destination
        .join(command.cwd.file_name().unwrap())
        .join("out/app/main/entry.js");
    assert_node_output(&entry, "RT\n", "");
}
