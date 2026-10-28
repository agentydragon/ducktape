use std::fs;
use std::process::Command;

use debundle_e2e_support::GraphFixture;
use serde_json::Value;
use tempfile::TempDir;

fn run_json(command: &mut Command) -> Value {
    let output = command.output().expect("run debundle CLI");
    assert!(
        output.status.success(),
        "stdout: {}\nstderr: {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("JSON invocation output")
}

fn path_argument<'a>(record: &'a Value, name: &str) -> &'a str {
    record["arguments"]
        .as_array()
        .unwrap()
        .iter()
        .find(|argument| argument["name"] == name)
        .unwrap_or_else(|| panic!("missing {name} in {record:#}"))["value"]["paths"][0]
        .as_str()
        .unwrap()
}

fn assert_shell_command_round_trips(record: &Value) {
    let command = shlex::split(record["shell_command"].as_str().unwrap())
        .expect("shell-safe invocation command");
    let argv = record["argv"]
        .as_array()
        .unwrap()
        .iter()
        .map(|value| value.as_str().unwrap().to_string())
        .collect::<Vec<_>>();
    assert_eq!(command[0], "cd");
    assert_eq!(command[1], record["cwd"].as_str().unwrap());
    assert_eq!(command[2], "&&");
    assert_eq!(command[3], "exec");
    assert_eq!(&command[4..], argv.as_slice());
}

fn replay_printed(record: &Value) -> Value {
    let argv = record["argv"].as_array().unwrap();
    let mut replay = Command::new(argv[0].as_str().unwrap());
    replay
        .args(argv[1..].iter().map(|value| value.as_str().unwrap()))
        .arg("--print-invocation")
        .current_dir(record["cwd"].as_str().unwrap());
    run_json(&mut replay)
}

#[test]
fn graph_environment_is_materialized_and_cli_flag_wins() {
    let fixture = GraphFixture::new("const a = 1; console.log(a);", &[]);
    let temp = TempDir::new().unwrap();
    let env_graph = temp.path().join("graph with ;$(touch not-a-command).json");
    let flag_graph = temp.path().join("flag graph.json");

    let mut env_only = fixture.process(&["gate", "list", "--print-invocation"]);
    env_only.env("DEBUNDLE_GRAPH", &env_graph);
    let env_record = run_json(&mut env_only);
    assert_eq!(env_record["mode"], "diagnostic_snapshot_read");
    assert_eq!(env_record["requested_proof"], "diagnostic_snapshot_input");
    assert_eq!(env_record["will_execute"], false);
    assert!(
        env_record["argv"]
            .as_array()
            .unwrap()
            .windows(2)
            .any(|pair| {
                pair[0] == "--graph" && pair[1] == env_graph.to_string_lossy().as_ref()
            })
    );
    assert_shell_command_round_trips(&env_record);

    // Replay after clearing the environment source. The resolved option keeps
    // the same effective graph input and no shell metacharacter is evaluated.
    let replay_argv = env_record["argv"].as_array().unwrap();
    let mut replay = Command::new(replay_argv[0].as_str().unwrap());
    replay
        .args(replay_argv[1..].iter().map(|value| value.as_str().unwrap()))
        .arg("--print-invocation")
        .current_dir(env_record["cwd"].as_str().unwrap())
        .env_remove("DEBUNDLE_GRAPH");
    let replay_record = run_json(&mut replay);
    assert_eq!(replay_record["argv"], env_record["argv"]);
    assert_eq!(
        path_argument(&replay_record, "dependency_graph_path"),
        env_graph.to_string_lossy()
    );
    assert!(!temp.path().join("not-a-command").exists());

    let mut overridden = fixture.process(&[
        "gate",
        "list",
        "--graph",
        flag_graph.to_str().unwrap(),
        "--print-invocation",
    ]);
    overridden.env("DEBUNDLE_GRAPH", &env_graph);
    let flag_record = run_json(&mut overridden);
    assert_eq!(flag_record["mode"], "diagnostic_snapshot_read");
    assert_eq!(flag_record["requested_proof"], "diagnostic_snapshot_input");
    assert!(
        flag_record["argv"]
            .as_array()
            .unwrap()
            .windows(2)
            .any(|pair| {
                pair[0] == "--graph" && pair[1] == flag_graph.to_string_lossy().as_ref()
            })
    );
    assert!(
        !flag_record["argv"]
            .as_array()
            .unwrap()
            .iter()
            .any(|value| value == env_graph.to_string_lossy().as_ref())
    );
}

#[test]
fn graph_environment_value_starting_with_dash_replays_as_option_value() {
    let fixture = GraphFixture::new("const a = 1; console.log(a);", &[]);

    let mut env_only = fixture.process(&["gate", "list", "--print-invocation"]);
    env_only.env("DEBUNDLE_GRAPH", "-graph.json");
    let record = run_json(&mut env_only);
    let argv = record["argv"].as_array().unwrap();
    assert!(argv.iter().any(|value| value == "--graph=-graph.json"));
    assert_eq!(
        path_argument(&record, "dependency_graph_path"),
        "-graph.json"
    );

    let mut replay = Command::new(argv[0].as_str().unwrap());
    replay
        .args(argv[1..].iter().map(|value| value.as_str().unwrap()))
        .arg("--print-invocation")
        .current_dir(record["cwd"].as_str().unwrap())
        .env_remove("DEBUNDLE_GRAPH");
    let replay_record = run_json(&mut replay);
    assert_eq!(replay_record["argv"], record["argv"]);
    assert_eq!(
        path_argument(&replay_record, "dependency_graph_path"),
        "-graph.json"
    );
}

#[test]
fn ambient_graph_does_not_misclassify_source_or_diagnostic_reads() {
    let fixture = GraphFixture::new("const a = 1; console.log(a);", &[]);
    let temp = TempDir::new().unwrap();
    let graph = temp.path().join("missing ambient graph.json");
    let source = temp.path().join("missing source.js");

    let mut inspect_source = fixture.process(&[
        "inspect-source",
        "--source-file",
        source.to_str().unwrap(),
        "--print-invocation",
    ]);
    inspect_source.env("DEBUNDLE_GRAPH", &graph);
    let inspect_record = run_json(&mut inspect_source);
    assert_eq!(inspect_record["mode"], "source_snapshot_read");
    assert_eq!(inspect_record["requested_proof"], "none");
    assert_eq!(inspect_record["will_execute"], false);

    for (args, expected_mode, expected_proof) in [
        (
            vec!["gate", "list", "--print-invocation"],
            "diagnostic_snapshot_read",
            "diagnostic_snapshot_input",
        ),
        (
            vec!["gate", "cut", "0", "--print-invocation"],
            "diagnostic_snapshot_read",
            "diagnostic_snapshot_input",
        ),
        (
            vec!["gate", "describe", "0", "--print-invocation"],
            "graph_snapshot_read",
            "graph_snapshot_input",
        ),
    ] {
        let mut command = fixture.process(&args);
        command.env("DEBUNDLE_GRAPH", &graph);
        let record = run_json(&mut command);
        assert_eq!(record["mode"], expected_mode, "{args:?}");
        assert_eq!(record["requested_proof"], expected_proof, "{args:?}");
        assert_eq!(record["will_execute"], false, "{args:?}");
    }

    assert!(!graph.exists());
    assert!(!source.exists());
    assert!(!temp.path().join("cycles.json").exists());
}

#[test]
fn print_only_reports_tree_roots_without_creating_outputs() {
    let fixture = GraphFixture::new("const a = 1; console.log(a);", &[]);
    let temp = TempDir::new().unwrap();
    let out_root = temp.path().join("out ;$(touch should-not-exist)");
    let source_root = temp.path().join("source root;$(touch also-not-created)");
    let config = temp.path().join("tree config.yaml");
    let modules = temp.path().join("module tree");
    let vendor_marks = temp.path().join("vendor marks.yaml");
    let package_root = temp.path().join("pkg ;$(touch package-marker)");
    let package_root_argument = format!("synthetic={}", package_root.display());

    let mut command = fixture.process(&[
        "run",
        "--tree-config",
        config.to_str().unwrap(),
        "--tree-modules",
        modules.to_str().unwrap(),
        "--tree-vendor-marks",
        vendor_marks.to_str().unwrap(),
        "--package-root",
        &package_root_argument,
        "--print-invocation",
    ]);
    command.env("DEBUNDLE_TREE_SOURCE_ROOT", &source_root);
    command.env("DEBUNDLE_OUT", &out_root);
    let record = run_json(&mut command);

    assert_eq!(record["mode"], "fresh_pipeline");
    assert_eq!(record["requested_proof"], "full_pipeline_requested");
    assert_eq!(record["write_scope"][0]["scope"], "tree_output");
    let argv = record["argv"].as_array().unwrap();
    for (flag, path) in [
        ("--tree-config", config.to_string_lossy().as_ref()),
        ("--tree-modules", modules.to_string_lossy().as_ref()),
        (
            "--tree-vendor-marks",
            vendor_marks.to_string_lossy().as_ref(),
        ),
        ("--tree-source-root", source_root.to_string_lossy().as_ref()),
        ("--out-root", out_root.to_string_lossy().as_ref()),
    ] {
        assert!(
            argv.windows(2)
                .any(|pair| pair[0] == flag && pair[1] == path)
        );
    }
    assert_eq!(
        path_argument(&record, "tree_vendor_marks"),
        vendor_marks.to_string_lossy()
    );
    assert!(
        argv.windows(2)
            .any(|pair| { pair[0] == "--package-root" && pair[1] == package_root_argument })
    );
    let package_roots = record["arguments"]
        .as_array()
        .unwrap()
        .iter()
        .find(|argument| argument["name"] == "package_roots")
        .unwrap_or_else(|| panic!("missing package_roots in {record:#}"));
    assert_eq!(
        package_roots["value"]["package_roots"][0]["package"],
        "synthetic"
    );
    assert_eq!(
        package_roots["value"]["package_roots"][0]["path"],
        package_root.to_string_lossy().as_ref()
    );
    assert_shell_command_round_trips(&record);
    assert!(!out_root.exists());
    assert!(!temp.path().join("should-not-exist").exists());
    assert!(!temp.path().join("also-not-created").exists());
    assert!(!temp.path().join("package-marker").exists());
}

#[test]
fn print_only_edit_and_capture_report_consistent_plans() {
    let fixture = GraphFixture::new(
        "const a = 1; console.log(a);",
        &[("home.yaml", "members: [{selector: {binding: {name: a}}}]\n")],
    );
    let module = fixture.modules.join("home.yaml");
    let before = fs::read(&module).unwrap();
    let print = fixture.command(&["bindings", "rename", "a", "renamed", "--print-invocation"]);
    assert!(print.status.success());
    let record: Value = serde_json::from_slice(&print.stdout).unwrap();
    assert_eq!(record["mode"], "validated_edit");
    assert_eq!(record["requested_proof"], "names_only_check_requested");
    assert_eq!(record["will_execute"], false);
    assert_eq!(fs::read(&module).unwrap(), before);
    assert!(!fixture.graph.with_file_name("cycles.json").exists());
    assert!(
        !fixture
            .graph
            .with_file_name("atomic_group_conflicts.json")
            .exists()
    );

    let unverified = fixture.command(&[
        "bindings",
        "assign",
        "a:other",
        "--no-verify",
        "--print-invocation",
    ]);
    assert!(unverified.status.success());
    let unverified: Value = serde_json::from_slice(&unverified.stdout).unwrap();
    assert_eq!(unverified["mode"], "unvalidated_edit");
    assert_eq!(unverified["requested_proof"], "none");
    assert_eq!(unverified["will_execute"], false);

    let temp = TempDir::new_in(fixture.modules.parent().unwrap()).unwrap();
    let invocation_path = temp.path().join("actual invocation.json");
    let preview = fixture.command(&["graph-summary", "--print-invocation"]);
    assert!(preview.status.success());
    let preview: Value = serde_json::from_slice(&preview.stdout).unwrap();
    let captured = fixture.command(&[
        "graph-summary",
        "--invocation-out",
        invocation_path.to_str().unwrap(),
    ]);
    assert!(
        captured.status.success(),
        "{}",
        String::from_utf8_lossy(&captured.stderr)
    );
    let actual: Value = serde_json::from_slice(&fs::read(&invocation_path).unwrap()).unwrap();
    assert_eq!(actual["argv"], preview["argv"]);
    assert_eq!(actual["arguments"], preview["arguments"]);
    assert_eq!(actual["mode"], preview["mode"]);
    assert_eq!(actual["will_execute"], true);
}

#[test]
fn option_looking_positional_text_after_double_dash_survives_capture_and_replay() {
    let fixture = GraphFixture::new(
        "const a = 1; console.log(a);",
        &[("home.yaml", "members: [{selector: {binding: {name: a}}}]\n")],
    );
    let invocation_path = fixture.modules.parent().unwrap().join("invocation.json");
    let print = fixture.command(&[
        "modules",
        "comment",
        "home",
        "--invocation-out",
        invocation_path.to_str().unwrap(),
        "--print-invocation",
        "--",
        "--invocation-out",
    ]);
    assert!(print.status.success());
    // The print-only option prevented the comment mutation, while capture
    // retained the option-looking positional text after the delimiter.
    let record: Value = serde_json::from_slice(&print.stdout).unwrap();
    let argv = record["argv"].as_array().unwrap();
    let delimiter = argv.iter().position(|value| value == "--").unwrap();
    assert_eq!(argv[delimiter + 1], "--invocation-out");
    assert_eq!(
        fs::read_to_string(fixture.modules.join("home.yaml")).unwrap(),
        "members: [{selector: {binding: {name: a}}}]\n"
    );

    let mut replay = Command::new(argv[0].as_str().unwrap());
    replay
        .args(argv[1..].iter().map(|value| value.as_str().unwrap()))
        .current_dir(record["cwd"].as_str().unwrap())
        .env_remove("DEBUNDLE_MODULES");
    let replayed = replay.output().expect("replay invocation argv");
    assert!(
        replayed.status.success(),
        "{}",
        String::from_utf8_lossy(&replayed.stderr)
    );
    let outcome: Value = serde_json::from_slice(&replayed.stdout).unwrap();
    assert_eq!(outcome["comment"], "--invocation-out");
}

#[test]
fn snapshot_print_only_materializes_global_roots_once_without_touching_inputs() {
    let fixture = GraphFixture::new("const a = 1; console.log(a);", &[]);
    let temp = TempDir::new().unwrap();
    let graph = temp.path().join("missing graph;$(no-shell).json");
    let destination = temp.path().join("new worker snapshot");
    let spec_root = temp.path().join("relocated spec root");
    let graph_source_root = temp.path().join("relocated graph source");

    let mut create = fixture.process(&[
        "snapshot",
        "create",
        "--destination",
        destination.to_str().unwrap(),
        "--print-invocation",
    ]);
    create
        .env("DEBUNDLE_GRAPH", &graph)
        .env("DEBUNDLE_SPEC_ROOT", &spec_root)
        .env("DEBUNDLE_GRAPH_SOURCE_ROOT", &graph_source_root);
    let record = run_json(&mut create);
    assert_eq!(record["mode"], "snapshot_creation");
    assert_eq!(record["requested_proof"], "snapshot_creation_requested");
    assert_eq!(record["will_execute"], false);
    assert_eq!(record["write_scope"][0]["scope"], "worker_snapshot");
    assert_eq!(
        record["write_scope"][0]["destination"],
        destination.to_string_lossy().as_ref()
    );
    let argv = record["argv"].as_array().unwrap();
    for (flag, path) in [
        ("--graph", graph.to_string_lossy().as_ref()),
        ("--destination", destination.to_string_lossy().as_ref()),
        ("--spec-root", spec_root.to_string_lossy().as_ref()),
        (
            "--graph-source-root",
            graph_source_root.to_string_lossy().as_ref(),
        ),
    ] {
        assert_eq!(
            argv.windows(2)
                .filter(|pair| pair[0] == flag && pair[1] == path)
                .count(),
            1,
            "expected exactly one {flag} {path:?} in {argv:#?}"
        );
    }
    assert_shell_command_round_trips(&record);

    let mut replay = Command::new(argv[0].as_str().unwrap());
    replay
        .args(argv[1..].iter().map(|value| value.as_str().unwrap()))
        .arg("--print-invocation")
        .current_dir(record["cwd"].as_str().unwrap())
        .env_remove("DEBUNDLE_GRAPH")
        .env_remove("DEBUNDLE_SPEC_ROOT")
        .env_remove("DEBUNDLE_GRAPH_SOURCE_ROOT");
    let replay_record = run_json(&mut replay);
    assert_eq!(replay_record["argv"], record["argv"]);
    assert!(!destination.exists());
    assert!(!graph.exists());

    let verify_directory = temp.path().join("missing snapshot directory");
    let mut verify = fixture.process(&[
        "snapshot",
        "verify",
        verify_directory.to_str().unwrap(),
        "--print-invocation",
    ]);
    verify
        .env("DEBUNDLE_SPEC_ROOT", &spec_root)
        .env("DEBUNDLE_GRAPH_SOURCE_ROOT", &graph_source_root);
    let verify_record = run_json(&mut verify);
    assert_eq!(verify_record["mode"], "snapshot_verification");
    assert_eq!(
        verify_record["requested_proof"],
        "snapshot_verification_requested"
    );
    assert_eq!(verify_record["will_execute"], false);
    assert_eq!(verify_record["write_scope"].as_array().unwrap().len(), 0);
    assert!(!verify_directory.exists());
}

#[test]
fn describe_default_limit_is_captured_and_replayable() {
    let fixture = GraphFixture::new("const a = 1; console.log(a);", &[]);
    let record = fixture.json(&["describe", "a", "--print-invocation"]);

    let limit = record["arguments"]
        .as_array()
        .unwrap()
        .iter()
        .find(|argument| argument["name"] == "limit")
        .expect("default limit is captured");
    assert_eq!(limit["source"], "default");
    assert_eq!(limit["value"]["values"][0], "20");
    let argv = record["argv"].as_array().unwrap();
    assert!(
        argv.windows(2)
            .any(|pair| pair[0] == "--limit" && pair[1] == "20")
    );

    let replay = replay_printed(&record);
    assert_eq!(replay["argv"], record["argv"]);
    let explicit = fixture.json(&["describe", "a", "--limit", "20", "--print-invocation"]);
    let explicit_limit = explicit["arguments"]
        .as_array()
        .unwrap()
        .iter()
        .find(|argument| argument["name"] == "limit")
        .expect("explicit limit is captured");
    assert_eq!(explicit_limit["source"], "command_line");
    assert_eq!(explicit_limit["value"]["values"][0], "20");
    assert_eq!(replay_printed(&explicit)["argv"], explicit["argv"]);
}

#[test]
fn describe_all_replays_without_a_conflicting_default_limit() {
    let fixture = GraphFixture::new("const a = 1; console.log(a);", &[]);
    let record = fixture.json(&["describe", "a", "--all", "--print-invocation"]);

    assert!(
        record["arguments"]
            .as_array()
            .unwrap()
            .iter()
            .all(|argument| argument["name"] != "limit")
    );
    assert!(
        record["argv"]
            .as_array()
            .unwrap()
            .iter()
            .any(|argument| argument == "--all")
    );
    assert!(
        !record["argv"]
            .as_array()
            .unwrap()
            .iter()
            .any(|argument| argument == "--limit" || argument == "--limit=20")
    );
    let replay = replay_printed(&record);
    assert_eq!(replay["argv"], record["argv"]);
    assert!(
        replay["argv"]
            .as_array()
            .unwrap()
            .iter()
            .any(|argument| argument == "--all")
    );
}
