//! Replay retains real source/spec inputs after their original owner is gone.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::*;
use std::fs;
use std::panic::catch_unwind;
use std::path::{Path, PathBuf};

fn failed_case() -> PathBuf {
    let cases = failed_fixture_dirs_for_current_test();
    let [case] = cases.as_slice() else {
        panic!("expected one case: {cases:?}");
    };
    let metadata = read_json::<serde_json::Value>(&case.join("case.json"));
    assert!(!Path::new(metadata["original_root"].as_str().unwrap()).exists());
    case.clone()
}

fn replay(case: &Path, command: &RecordedCommand) -> (CaseWorkspace, std::process::Output) {
    let workspace = CaseWorkspace::with_retention_root(None);
    let output = replay_command(
        case,
        &command.id,
        &workspace.path().join("replayed"),
        &command.program,
        false,
    )
    .unwrap();
    (workspace, output)
}

#[test]
fn unexpected_cli_exit_replays_after_the_original_directory_is_removed() {
    assert!(
        catch_unwind(|| {
            run_rejection_fixture_with_args(FixtureOpts::new("console.log(7);", vec![]), &[], 3);
        })
        .is_err()
    );
    let case = failed_case();
    let commands = recorded_commands(&case).unwrap();
    let command = commands.iter().find(|command| command.debundle).unwrap();
    assert_eq!(command.exit_code, Some(0));
    let (workspace, output) = replay(&case, command);
    process::assert_success(&output);
    assert_node_output(
        &workspace
            .path()
            .join("replayed/out/app/static/app/entry.js"),
        "7\n",
        "",
    );
}

#[test]
fn runtime_mismatch_retains_and_replays_both_node_observations() {
    assert!(
        catch_unwind(|| {
            let fixture = run_fixture(FixtureOpts::new("console.log(8);", vec![]));
            write_text_file(&fixture.entry_path, "console.log(9);");
            assert_preserved_entry_output(&fixture, "8\n");
        })
        .is_err()
    );
    let case = failed_case();
    let commands = recorded_commands(&case).unwrap();
    let runtime: Vec<_> = commands
        .iter()
        .filter(|command| !command.debundle)
        .collect();
    assert_eq!(runtime.len(), 2);
    for (command, expected) in runtime.into_iter().zip([b"8\n", b"9\n"]) {
        let bytes = fs::read(case.join(".case-commands").join(&command.id).join("stdout")).unwrap();
        assert_eq!(bytes, expected);
        let (_workspace, output) = replay(&case, command);
        assert!(output.status.success());
        assert_eq!(output.stdout, bytes);
    }
}

#[test]
fn tree_command_replays_from_the_retained_relative_layout() {
    assert!(
        catch_unwind(|| {
            let run = run_tree_fixture(
                &TreeFixture {
                    chunks: &[("main", "console.log(9);")],
                    module_roots: &[],
                    modules: &[],
                },
                &[],
            );
            assert_node_output(&run.out_root.join("app/main/entry.js"), "incorrect\n", "");
        })
        .is_err()
    );
    let case = failed_case();
    let command = recorded_commands(&case)
        .unwrap()
        .into_iter()
        .find(|command| command.debundle)
        .unwrap();
    let (workspace, output) = replay(&case, &command);
    process::assert_success(&output);
    assert_node_output(
        &workspace.path().join("replayed/out/app/main/entry.js"),
        "9\n",
        "",
    );
}

#[test]
fn replay_preserves_an_intentional_nonempty_output_refusal() {
    assert!(
        catch_unwind(|| {
            let workspace = write_tree_fixture(&TreeFixture {
                chunks: &[("main", "console.log(9);")],
                module_roots: &[],
                modules: &[],
            });
            write_text_file(
                &workspace.root().join("out/sentinel"),
                "original output state",
            );
            let run = workspace.run(&[]);
            assert!(
                run.result.status.success(),
                "deliberately unexpected refusal"
            );
        })
        .is_err()
    );
    let case = failed_case();
    let command = recorded_commands(&case).unwrap().remove(0);
    assert_eq!(command.exit_code, Some(1));
    let (workspace, output) = replay(&case, &command);
    assert_eq!(output.status.code(), command.exit_code);
    assert!(String::from_utf8_lossy(&output.stderr).contains("Output directory is not empty"));
    assert_eq!(
        fs::read_to_string(workspace.path().join("replayed/out/sentinel")).unwrap(),
        "original output state"
    );
}

#[test]
fn staged_artifact_and_its_invocation_spec_replay_without_the_setup_owner() {
    assert!(
        catch_unwind(|| {
            let (staged, _fixture) =
                saved_analysis::staged(FixtureOpts::new("console.log(10);", vec![]));
            let run = staged.run(&["--analysis", saved_analysis::path(&staged.artifact)]);
            assert_node_output(
                &run.output_root().join("app/static/app/entry.js"),
                "incorrect\n",
                "",
            );
        })
        .is_err()
    );
    let case = failed_case();
    let command = recorded_commands(&case)
        .unwrap()
        .into_iter()
        .find(|command| command.debundle && command.args.iter().any(|arg| arg == "--analysis"))
        .unwrap();
    let (_workspace, output) = replay(&case, &command);
    process::assert_success(&output);
}

#[test]
fn earlier_projection_replays_its_original_source_after_a_later_edit() {
    assert!(
        catch_unwind(|| {
            let (staged, _fixture) =
                saved_analysis::staged(FixtureOpts::new("console.log(11);", vec![]));
            write_text_file(
                &staged.root().join("snapshot/static/app.js"),
                "this is deliberately invalid JS !!!",
            );
            panic!("later assertion");
        })
        .is_err()
    );
    let case = failed_case();
    let command = recorded_commands(&case).unwrap().remove(0);
    let (workspace, output) = replay(&case, &command);
    process::assert_success(&output);
    assert_eq!(
        fs::read_to_string(workspace.path().join("replayed/snapshot/static/app.js")).unwrap(),
        "console.log(11);"
    );
}

#[test]
fn replay_requires_an_explicit_matching_tool_identity() {
    assert!(
        catch_unwind(|| {
            run_rejection_fixture_with_args(FixtureOpts::new("console.log(12);", vec![]), &[], 3);
        })
        .is_err()
    );
    let case = failed_case();
    let command = recorded_commands(&case).unwrap().remove(0);
    let workspace = CaseWorkspace::with_retention_root(None);
    let error = replay_command(
        &case,
        &command.id,
        &workspace.path().join("replayed"),
        Path::new("/bin/sh"),
        false,
    )
    .unwrap_err();
    assert!(error.to_string().contains("executable differs"));
    assert!(!workspace.path().join("replayed").exists());
}

#[test]
fn graph_query_replays_with_its_recorded_graph_and_child_environment() {
    assert!(
        catch_unwind(|| {
            let fixture = GraphFixture::acyclic_pair();
            let report = fixture.json(&["describe", "alpha"]);
            assert_eq!(report["query"]["value"], "deliberately incorrect");
        })
        .is_err()
    );
    let case = failed_case();
    let command = recorded_commands(&case)
        .unwrap()
        .into_iter()
        .find(|command| command.args.first().is_some_and(|arg| arg == "describe"))
        .unwrap();
    assert_eq!(command.exit_code, Some(0));
    let (_workspace, output) = replay(&case, &command);
    process::assert_success(&output);
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["query"]["value"], "alpha");
}

#[test]
fn embedded_loader_dependency_replays_after_its_external_owner_is_gone() {
    let dependency = CaseWorkspace::with_retention_root(None);
    let manifest = dependency.path().join("loaders.json");
    write_text_file(&manifest, "{}");
    assert!(
        catch_unwind(|| {
            let fixture = run_fixture(
                FixtureOpts::new("console.log(13);", vec![])
                    .with_embedded_loaders_path(manifest.clone()),
            );
            assert_entry_output(&fixture, "incorrect\n");
        })
        .is_err()
    );
    drop(dependency);
    assert!(!manifest.exists());
    let case = failed_case();
    let command = recorded_commands(&case).unwrap().remove(0);
    let (workspace, output) = replay(&case, &command);
    process::assert_success(&output);
    assert_node_output(
        &workspace
            .path()
            .join("replayed/out/app/static/app/entry.js"),
        "13\n",
        "",
    );
}

#[test]
fn nonzero_status_and_non_utf8_streams_survive_retention_and_replay() {
    assert!(
        catch_unwind(|| {
            let workspace = CaseWorkspace::default();
            let output = fixture_output(
                std::process::Command::new("/bin/sh")
                    .args(["-c", "printf '\\377'; printf '\\376' >&2; exit 17"]),
                workspace.path(),
            )
            .unwrap();
            assert!(
                output.status.success(),
                "deliberate nonzero-status mismatch"
            );
        })
        .is_err()
    );
    let case = failed_case();
    let command = recorded_commands(&case).unwrap().remove(0);
    assert_eq!(command.exit_code, Some(17));
    let (_workspace, output) = replay(&case, &command);
    assert_eq!(output.status.code(), Some(17));
    assert_eq!(output.stdout, [255]);
    assert_eq!(output.stderr, [254]);
}

#[test]
fn invariant_guard_panics_after_raw_output_has_been_recorded() {
    assert!(
        catch_unwind(|| {
            let workspace = CaseWorkspace::default();
            debundle_fixture_output(
                std::process::Command::new("/bin/sh")
                    .args(["-c", "printf 'purity-completion-disagreement' >&2; exit 1"]),
                workspace.path(),
            )
            .unwrap();
        })
        .is_err()
    );
    let case = failed_case();
    let command = recorded_commands(&case).unwrap().remove(0);
    assert_eq!(command.exit_code, Some(1));
    assert_eq!(
        fs::read(case.join(".case-commands").join(command.id).join("stderr")).unwrap(),
        b"purity-completion-disagreement"
    );
}

#[test]
fn an_input_containing_the_journal_does_not_recursively_copy_itself() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new("console.log(22);", vec![]));
    let workspace = fixture.workspace();
    let root = workspace.path();
    fs::create_dir_all(root.join("static")).unwrap();
    fs::copy(
        root.join("snapshot/static/app.js"),
        root.join("static/app.js"),
    )
    .unwrap();
    let mut spec: serde_json::Value =
        serde_yaml::from_slice(&fs::read(&fixture.spec_path).unwrap()).unwrap();
    spec["inputs"]["input_root"] = ".".into();
    write_yaml_file(&fixture.spec_path, &spec);

    let result = run_debundler(&fixture.spec_path, &[]);
    assert!(result.status.success(), "{}", result.stderr);
    assert_node_output(&root.join("out/app/static/app/entry.js"), "22\n", "");
    let command = recorded_commands(root)
        .unwrap()
        .into_iter()
        .find(|command| command.debundle)
        .unwrap();
    assert!(
        command
            .snapshot_errors
            .iter()
            .any(|error| error.contains("contains the command journal")),
        "{:?}",
        command.snapshot_errors
    );
    let destination = root.join("unused-replay");
    let error =
        replay_command(root, &command.id, &destination, &command.program, false).unwrap_err();
    assert!(error.to_string().contains("incomplete input snapshot"));
    assert!(!destination.exists());
}
