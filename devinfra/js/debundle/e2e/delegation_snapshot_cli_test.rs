use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Output};

use debundle_e2e_support::{TreeFixture, debundler_path, run_tree_fixture};
use serde_json::{Value, json};
use tempfile::TempDir;

fn checked(command: &mut Command) -> Value {
    let output = command.output().expect("execute debundle");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("structured command result")
}

fn create(graph: &Path, destination: &Path) -> Value {
    checked(
        Command::new(debundler_path())
            .args(["snapshot", "create", "--graph"])
            .arg(graph)
            .arg("--destination")
            .arg(destination),
    )
}

fn verify(directory: &Path) -> Output {
    Command::new(debundler_path())
        .args(["snapshot", "verify"])
        .arg(directory)
        .output()
        .expect("verify snapshot")
}

fn app_files(root: &Path) -> std::collections::BTreeMap<std::path::PathBuf, Vec<u8>> {
    fn collect(
        root: &Path,
        directory: &Path,
        files: &mut std::collections::BTreeMap<std::path::PathBuf, Vec<u8>>,
    ) {
        for entry in fs::read_dir(directory).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                collect(root, &path, files);
            } else {
                files.insert(
                    path.strip_prefix(root).unwrap().to_path_buf(),
                    fs::read(path).unwrap(),
                );
            }
        }
    }

    let mut files = std::collections::BTreeMap::new();
    collect(root, root, &mut files);
    files
}

#[test]
fn delegated_workers_replay_after_original_inputs_are_removed_and_edit_independently() {
    let packages = TempDir::new().unwrap();
    let fixture = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", "const value = 7; console.log(value);\n")],
            module_roots: &[("main", "main")],
            modules: &[(
                "main/value.yaml",
                "members: [{selector: {binding: {name: value}}}]\n",
            )],
        },
        &[
            "--packages-root",
            packages.path().to_str().unwrap(),
            "--fail-fast",
        ],
    );
    assert!(fixture.result.status.success(), "{}", fixture.result.stderr);
    let workers = TempDir::new().unwrap();
    let first = workers.path().join("first worker");
    let second = workers.path().join("second worker");
    let graph = fixture.report_root.join("main/dependency_graph.json");
    let first_report = create(&graph, &first);
    create(&graph, &second);
    fs::remove_dir_all(fixture.root()).unwrap();
    packages.close().unwrap();

    assert!(verify(&first).status.success());
    let moved_second = workers.path().join("moved second worker");
    fs::rename(&second, &moved_second).unwrap();
    assert!(verify(&moved_second).status.success());
    let query = checked(
        Command::new(first_report["engine"].as_str().unwrap())
            .arg("--graph-package-root")
            .arg(format!(
                "__packages_root__={}",
                first_report["graph_package_roots"]["__packages_root__"]
                    .as_str()
                    .unwrap()
            ))
            .args([
                "--spec-root",
                first_report["spec_root"].as_str().unwrap(),
                "--graph-source-root",
                first_report["graph_source_root"].as_str().unwrap(),
                "describe",
                "value",
                "--graph",
                first_report["graph"].as_str().unwrap(),
                "--source-root",
                first_report["source_root"].as_str().unwrap(),
                "--modules",
            ])
            .arg(Path::new(first_report["modules"].as_str().unwrap()).join("main"))
            .args(["--format", "json"]),
    );
    assert!(!query["source_unit_ids"].as_array().unwrap().is_empty());
    let argv: Vec<_> = first_report["regeneration_argv"]
        .as_array()
        .unwrap()
        .iter()
        .map(|arg| arg.as_str().unwrap())
        .collect();
    assert!(argv.contains(&"--packages-root"));
    assert!(argv.contains(&"--fail-fast"));
    assert!(!argv.iter().any(|arg| arg.starts_with("__packages_root__=")));
    let replay = Command::new(argv[0]).args(&argv[1..]).output().unwrap();
    assert!(
        replay.status.success(),
        "{}",
        String::from_utf8_lossy(&replay.stderr)
    );
    assert!(first.join("output/app/main/entry.js").is_file());

    let modules = Path::new(first_report["modules"].as_str().unwrap());
    let module = modules.join("main/value.yaml");
    let mut yaml = fs::read_to_string(&module).unwrap();
    yaml.push_str("note: worker edit\n");
    fs::write(module, yaml).unwrap();
    assert!(!verify(&first).status.success());
    assert!(verify(&moved_second).status.success());
}

#[test]
fn snapshot_replays_bundled_swap_with_existence_only_package_input() {
    let fixture = run_tree_fixture(
        &TreeFixture {
            chunks: &[(
                "main",
                "export const value = () => 3; export const directoryValue = () => 4;\n",
            )],
            module_roots: &[("main", "main")],
            modules: &[("main/empty.yaml", "members: []\n")],
        },
        &[],
    );
    assert!(fixture.result.status.success(), "{}", fixture.result.stderr);

    let package = TempDir::new().unwrap();
    fs::create_dir_all(package.path().join("dist")).unwrap();
    fs::write(
        package.path().join("package.json"),
        r#"{"name":"fixture-package","version":"1.0.0","type":"module"}"#,
    )
    .unwrap();
    fs::write(package.path().join("dist/index.js"), [0x00, 0xff, 0x01]).unwrap();
    let directory_package = TempDir::new().unwrap();
    fs::create_dir_all(directory_package.path().join("dist")).unwrap();
    fs::write(
        directory_package.path().join("package.json"),
        r#"{"name":"fixture-directory-package","version":"1.0.0","type":"module"}"#,
    )
    .unwrap();
    fs::write(
        fixture.root().join("bundle.js"),
        "const packageDefault = () => 3; const directoryDefault = () => 4;\nexport { packageDefault, directoryDefault };\n",
    )
    .unwrap();
    fs::write(
        fixture.root().join("vendor_marks.yaml"),
        serde_json::to_vec(&json!({
            "vendor_marks": [{
                "chunk_path": "main.js",
                "identity": "fixture package bundle",
                "level": "bundled_partial_swap",
                "bundle": {"path": "bundle.js"},
                "packages": {
                    "fixture-package": {
                        "version": "1.0.0",
                        "subpath": "dist/index.js",
                        "bundle_export": "packageDefault"
                    },
                    "fixture-directory-package": {
                        "version": "1.0.0",
                        "subpath": "dist",
                        "bundle_export": "directoryDefault"
                    }
                },
                "symbols": {
                    "value": {"package": "fixture-package", "kind": "default"},
                    "directoryValue": {"package": "fixture-directory-package", "kind": "default"}
                }
            }]
        }))
        .unwrap(),
    )
    .unwrap();

    fs::remove_dir_all(&fixture.out_root).unwrap();
    let original = Command::new(debundler_path())
        .args(["run", "--tree-config"])
        .arg(fixture.root().join("spec_config.yaml"))
        .arg("--tree-modules")
        .arg(fixture.root().join("modules"))
        .arg("--tree-vendor-marks")
        .arg(fixture.root().join("vendor_marks.yaml"))
        .arg("--tree-source-root")
        .arg(fixture.root())
        .arg("--package-root")
        .arg(format!("fixture-package={}", package.path().display()))
        .arg("--package-root")
        .arg(format!(
            "fixture-directory-package={}",
            directory_package.path().display()
        ))
        .arg("--out-root")
        .arg(&fixture.out_root)
        .output()
        .unwrap();
    assert!(
        original.status.success(),
        "{}",
        String::from_utf8_lossy(&original.stderr)
    );
    let original_app = app_files(&fixture.out_root.join("app"));

    let workers = TempDir::new().unwrap();
    let destination = workers.path().join("worker");
    let report = create(
        &fixture.report_root.join("main/dependency_graph.json"),
        &destination,
    );
    package.close().unwrap();
    directory_package.close().unwrap();
    assert!(verify(&destination).status.success());

    let argv: Vec<_> = report["regeneration_argv"]
        .as_array()
        .unwrap()
        .iter()
        .map(|arg| arg.as_str().unwrap())
        .collect();
    let replay = Command::new(argv[0]).args(&argv[1..]).output().unwrap();
    assert!(
        replay.status.success(),
        "{}",
        String::from_utf8_lossy(&replay.stderr)
    );
    let snapshot_file = Path::new(
        report["graph_package_roots"]["fixture-package"]
            .as_str()
            .unwrap(),
    )
    .join("dist/index.js");
    assert_eq!(fs::read(snapshot_file).unwrap(), [0x00, 0xff, 0x01]);
    let snapshot_directory = Path::new(
        report["graph_package_roots"]["fixture-directory-package"]
            .as_str()
            .unwrap(),
    )
    .join("dist");
    assert!(snapshot_directory.is_dir());
    assert_eq!(fs::read_dir(&snapshot_directory).unwrap().count(), 0);
    assert_eq!(original_app, app_files(&destination.join("output/app")));

    fs::remove_dir(&snapshot_directory).unwrap();
    fs::write(&snapshot_directory, b"directory replaced by a file").unwrap();
    let stale = verify(&destination);
    assert!(!stale.status.success());
    assert!(
        String::from_utf8_lossy(&stale.stderr).contains("actual: \"file\""),
        "{}",
        String::from_utf8_lossy(&stale.stderr)
    );
}

#[test]
fn stale_inputs_and_existing_destinations_do_not_publish_or_overwrite_snapshots() {
    let fixture = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", "const value = 1;\n")],
            module_roots: &[("main", "main")],
            modules: &[(
                "main/value.yaml",
                "members: [{selector: {binding: {name: value}}}]\n",
            )],
        },
        &[],
    );
    assert!(fixture.result.status.success(), "{}", fixture.result.stderr);
    let workers = TempDir::new().unwrap();
    let destination = workers.path().join("worker");
    let graph = fixture.report_root.join("main/dependency_graph.json");
    create(&graph, &destination);
    let before = fs::read(destination.join("snapshot.json")).unwrap();
    let repeated = Command::new(debundler_path())
        .args(["snapshot", "create", "--graph"])
        .arg(&graph)
        .arg("--destination")
        .arg(&destination)
        .output()
        .unwrap();
    assert!(!repeated.status.success());
    assert_eq!(fs::read(destination.join("snapshot.json")).unwrap(), before);

    fs::write(
        fixture.root().join("snapshot/main.js"),
        "const value = 2;\n",
    )
    .unwrap();
    let stale_destination = workers.path().join("stale worker");
    let stale = Command::new(debundler_path())
        .args(["snapshot", "create", "--graph"])
        .arg(&graph)
        .arg("--destination")
        .arg(&stale_destination)
        .output()
        .unwrap();
    assert!(!stale.status.success());
    assert!(!stale_destination.exists());
    assert!(verify(&destination).status.success());
}

#[test]
fn snapshot_engine_and_yaml_membership_are_verified() {
    let fixture = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", "const value = 1;\n")],
            module_roots: &[("main", "main")],
            modules: &[(
                "main/value.yaml",
                "members: [{selector: {binding: {name: value}}}]\n",
            )],
        },
        &[],
    );
    assert!(fixture.result.status.success(), "{}", fixture.result.stderr);
    let workers = TempDir::new().unwrap();
    let destination = workers.path().join("worker");
    let report = create(
        &fixture.report_root.join("main/dependency_graph.json"),
        &destination,
    );
    let modules = Path::new(report["modules"].as_str().unwrap());
    let added = modules.join("main/extra.yaml");
    fs::write(&added, "members: []\n").unwrap();
    assert!(!verify(&destination).status.success());
    fs::remove_file(added).unwrap();
    assert!(verify(&destination).status.success());
    let engine = report["engine"].as_str().unwrap();
    let permissions = fs::metadata(engine).unwrap().permissions();
    fs::set_permissions(
        engine,
        fs::Permissions::from_mode(permissions.mode() | 0o200),
    )
    .unwrap();
    fs::write(engine, "changed engine bytes").unwrap();
    assert!(!verify(&destination).status.success());
}

#[test]
fn browser_snapshot_replay_keeps_html_assets_and_summary_after_original_removal() {
    let fixture = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", "console.log(7);\n")],
            module_roots: &[("main", "main")],
            modules: &[("main/empty.yaml", "members: []\n")],
        },
        &[],
    );
    assert!(fixture.result.status.success(), "{}", fixture.result.stderr);
    let config = fixture.root().join("spec_config.yaml");
    let mut yaml = fs::read_to_string(&config).unwrap();
    yaml.push_str("browser_harness:\n  asset_summary_path: assets.json\n");
    fs::write(&config, yaml).unwrap();
    fs::write(
        fixture.root().join("assets.json"),
        r#"{"entryPoints":{"html":"index.html"}}"#,
    )
    .unwrap();
    fs::write(fixture.root().join("snapshot/index.html"),
        "<!doctype html><html><head><link rel=\"stylesheet\" href=\"style.css\"></head><body><script type=\"module\" src=\"main.js\"></script></body></html>").unwrap();
    fs::write(
        fixture.root().join("snapshot/style.css"),
        "body { color: navy; }\n",
    )
    .unwrap();
    fs::remove_dir_all(&fixture.out_root).unwrap();
    let original = Command::new(debundler_path())
        .args(["run", "--tree-config"])
        .arg(config)
        .arg("--tree-modules")
        .arg(fixture.root().join("modules"))
        .arg("--tree-vendor-marks")
        .arg(fixture.root().join("vendor_marks.yaml"))
        .arg("--tree-source-root")
        .arg(fixture.root())
        .arg("--out-root")
        .arg(&fixture.out_root)
        .output()
        .unwrap();
    assert!(
        original.status.success(),
        "{}",
        String::from_utf8_lossy(&original.stderr)
    );

    let workers = TempDir::new().unwrap();
    let destination = workers.path().join("browser worker");
    let nested_destination = fixture.root().join("snapshot/worker");
    let nested = Command::new(debundler_path())
        .args(["snapshot", "create", "--graph"])
        .arg(fixture.report_root.join("main/dependency_graph.json"))
        .arg("--destination")
        .arg(&nested_destination)
        .output()
        .unwrap();
    assert!(!nested.status.success());
    assert!(!nested_destination.exists());
    let report = create(
        &fixture.report_root.join("main/dependency_graph.json"),
        &destination,
    );
    fs::remove_dir_all(fixture.root()).unwrap();
    assert!(verify(&destination).status.success());
    let argv: Vec<_> = report["regeneration_argv"]
        .as_array()
        .unwrap()
        .iter()
        .map(|arg| arg.as_str().unwrap())
        .collect();
    let replay = Command::new(argv[0]).args(&argv[1..]).output().unwrap();
    assert!(
        replay.status.success(),
        "{}",
        String::from_utf8_lossy(&replay.stderr)
    );
    assert!(destination.join("output/app/index.html").is_file());
    assert_eq!(
        fs::read_to_string(destination.join("output/app/style.css")).unwrap(),
        "body { color: navy; }\n"
    );
    let extra = Path::new(report["source_root"].as_str().unwrap()).join("extra.css");
    fs::write(extra, "body {}\n").unwrap();
    assert!(!verify(&destination).status.success());
}
