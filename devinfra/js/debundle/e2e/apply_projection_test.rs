//! Default apply caching must preserve emission, report notes, and native
//! compatibility while full graphs retain exact authored input provenance.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use debundle_e2e_support::saved_analysis::*;
use debundle_e2e_support::*;
use serde_json::Value;

fn project(spec: &Path, out: &Path) -> Vec<u8> {
    assert_ok(
        &command(&[
            "spec",
            "project-apply",
            "--spec",
            path(spec),
            "--out",
            path(out),
        ]),
        "project apply",
    );
    fs::read(out).unwrap()
}

// Unlike saved_analysis::tree, retain every provenance field and byte.
fn exact_tree(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    fn visit(root: &Path, dir: &Path, files: &mut BTreeMap<PathBuf, Vec<u8>>) {
        for entry in fs::read_dir(dir).unwrap() {
            let entry = entry.unwrap().path();
            if entry.is_dir() {
                visit(root, &entry, files);
            } else {
                files.insert(
                    entry.strip_prefix(root).unwrap().to_owned(),
                    fs::read(entry).unwrap(),
                );
            }
        }
    }
    let mut files = BTreeMap::new();
    visit(root, root, &mut files);
    files
}

fn assert_projected_parity(opts: FixtureOpts<'_>, extra: &[&str], expected_status: i32) {
    let (staged, _fixture) = staged(opts);
    let direct = staged.run_without_graphs(extra);
    assert_eq!(
        direct.result.status.code(),
        Some(expected_status),
        "{}",
        direct.result.stderr
    );
    let expected = exact_tree(&direct.output_root());
    let expected_harness = direct.harness_root().map(exact_tree);
    let spec = direct.invocation_spec().unwrap();
    let projection = staged.root().join("apply.json");
    project(spec, &projection);
    fs::remove_dir_all(direct.output_root()).unwrap();
    if let Some(root) = direct.harness_root() {
        fs::remove_dir_all(root).unwrap();
    }
    // The core action does not receive authored YAML. A raw-YAML dependency
    // hidden from Bazel would fail here even outside an enforcing sandbox.
    fs::rename(spec, spec.with_extension("held")).unwrap();
    let mut args = vec![
        "run",
        "--spec",
        path(spec),
        "--analysis",
        path(&staged.artifact),
        "--apply-projection",
        path(&projection),
    ];
    args.extend(extra);
    let projected = command(&args);
    assert_eq!(
        projected.status.code(),
        Some(expected_status),
        "{}",
        projected.stderr
    );
    assert_eq!(exact_tree(&direct.output_root()), expected);
    assert_eq!(direct.harness_root().map(exact_tree), expected_harness);
}

#[test]
fn projected_apply_preserves_accepted_output_and_html_provenance() {
    assert_projected_parity(
        FixtureOpts::new(
            "const value = 42; console.log(value); export { value };",
            vec![logical_module("values", &[Member::new("value")])],
        )
        .with_browser_harness(),
        &["--full-report", "export_reentry"],
        0,
    );
}

#[test]
fn projected_apply_preserves_marked_rejected_output() {
    assert_projected_parity(
        rejected_split(),
        &[
            "--emit-on-rejection",
            "--full-report",
            "translation_validation",
        ],
        3,
    );
}

#[test]
fn projection_ignores_only_module_notes_and_yaml_presentation() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "export const value = 42;",
        vec![logical_module("values", &[Member::new("value")])],
    ));
    let projection = staged.root().join("apply.json");
    let original = project(&staged.spec, &projection);
    let mut spec: spec::TransformSpec =
        serde_yaml::from_slice(&fs::read(&staged.spec).unwrap()).unwrap();
    spec.logical_modules
        .get_mut("static/app")
        .unwrap()
        .get_mut("values")
        .unwrap()
        .note = Some("new author-only context".into());
    fs::write(
        &staged.spec,
        format!(
            "# changed presentation\n\n{}",
            serde_yaml::to_string(&spec).unwrap()
        ),
    )
    .unwrap();
    assert_eq!(project(&staged.spec, &projection), original);
    spec.logical_modules
        .get_mut("static/app")
        .unwrap()
        .get_mut("values")
        .unwrap()
        .comment = Some("emitted comment".into());
    fs::write(&staged.spec, serde_yaml::to_string(&spec).unwrap()).unwrap();
    let with_comment = project(&staged.spec, &projection);
    assert_ne!(with_comment, original);
    spec.host_behavior.note = Some("current host audit note".into());
    fs::write(&staged.spec, serde_yaml::to_string(&spec).unwrap()).unwrap();
    assert_ne!(project(&staged.spec, &projection), with_comment);
    let run = command(&[
        "run",
        "--spec",
        path(&staged.spec),
        "--analysis",
        path(&staged.artifact),
        "--apply-projection",
        path(&projection),
        "--full-report",
        "export_reentry",
    ]);
    assert_ok(&run, "projected run with current report note");
    let out = &spec.write_js_tree.as_ref().unwrap().out_dir;
    assert_source_file(
        &out.join("app/static/app/modules/values.js"),
        &["emitted comment"],
        &["new author-only context"],
    );
    let report: Value = serde_json::from_slice(
        &fs::read(out.join("reports/tree/static/app/export_reentry.full.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(report["host_behavior"]["note"], "current host audit note");
    let source = spec.inputs.input_root.join("static/app.js");
    fs::write(&source, "export const value = 43;").unwrap();
    let stale = command(&[
        "run",
        "--spec",
        path(&staged.spec),
        "--analysis",
        path(&staged.artifact),
        "--apply-projection",
        path(&projection),
    ]);
    assert_eq!(stale.status.code(), Some(1), "{}", stale.stderr);
    assert!(
        stale.stderr.contains("other parser inputs"),
        "{}",
        stale.stderr
    );
}

#[test]
fn tree_projection_preserves_full_graph_authored_inventory() {
    let staged = staged_tree(
        &[("main", "export const value = 42;")],
        &[("values", "members: [value]\n")],
    );
    let out = staged.root().join("apply-out");
    let projection = staged.root().join("apply.json");
    let context = staged.analysis_args(&staged.artifact, &out);
    let project_args: Vec<&str> = ["spec", "project-apply"]
        .into_iter()
        .chain(context[..10].iter().map(String::as_str))
        .chain(["--out", path(&projection)])
        .collect();
    assert_ok(&command(&project_args), "project tree apply");
    let before = fs::read(&projection).unwrap();
    let module = staged.modules.join("values.yaml");
    fs::write(
        &module,
        format!(
            "# current source\n{}\nnote: revised provenance\n",
            fs::read_to_string(&module).unwrap()
        ),
    )
    .unwrap();
    assert_ok(&command(&project_args), "project edited tree apply");
    assert_eq!(fs::read(&projection).unwrap(), before);
    let args: Vec<&str> = ["run"]
        .into_iter()
        .chain(context.iter().map(String::as_str))
        .chain(["--apply-projection", path(&projection)])
        .collect();
    assert_ok(&command(&args), "apply tree projection");
    let mut forbidden = args.clone();
    forbidden.push("--graphs");
    assert!(
        !command(&forbidden).status.success(),
        "projected inputs must not produce replayable graphs"
    );
    let graph = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&graph.result, "graph export with current authored input");
    let report: Value = read_gzip_json(
        &graph
            .output_root()
            .join("reports/tree/program_graph.json.gz"),
    );
    let files = report["provenance"]["files"].as_array().unwrap();
    let authored = files
        .iter()
        .find(|file| file["path"] == "modules/values.yaml")
        .expect("authored module inventory");
    assert_eq!(
        authored["sha256"],
        graph_provenance::sha256(&fs::read(&module).unwrap())
    );
}

#[test]
fn projection_and_analysis_transfer_between_execution_roots() {
    let staged = staged_tree(
        &[("main", "export const value = 42;")],
        &[("values", "members: [value]\n")],
    );
    let out = staged.root().join("projected-out");
    let projection = staged.root().join("apply.json");
    let context = staged.analysis_args(&staged.artifact, &out);
    let root = path(staged.root());
    let project_args: Vec<&str> = ["spec", "project-apply"]
        .into_iter()
        .chain(context[..10].iter().map(String::as_str))
        .chain(["--projection-root", root, "--out", path(&projection)])
        .collect();
    assert_ok(&command(&project_args), "first execution root projection");
    let first = fs::read(&projection).unwrap();
    assert!(
        !String::from_utf8_lossy(&first).contains(root),
        "projection captured the worker root"
    );
    let relocated = staged.root().join("relocated");
    fs::create_dir(&relocated).unwrap();
    for file in [
        &staged.config,
        &staged.vendor_marks,
        &staged.artifact,
        &projection,
    ] {
        fs::copy(file, relocated.join(file.file_name().unwrap())).unwrap();
    }
    for dir in [staged.root().join("snapshot"), staged.modules.clone()] {
        for (relative, bytes) in exact_tree(&dir) {
            let destination = relocated.join(dir.file_name().unwrap()).join(relative);
            fs::create_dir_all(destination.parent().unwrap()).unwrap();
            fs::write(destination, bytes).unwrap();
        }
    }
    fs::copy(
        staged.root().join("js-files.txt"),
        relocated.join("js-files.txt"),
    )
    .unwrap();
    let relocated_args: Vec<String> = project_args
        .iter()
        .map(|arg| arg.replace(root, path(&relocated)))
        .collect();
    assert_ok(
        &command(
            &relocated_args
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>(),
        ),
        "second execution root projection",
    );
    assert_eq!(fs::read(relocated.join("apply.json")).unwrap(), first);
    fs::remove_file(relocated.join(staged.config.file_name().unwrap())).unwrap();
    fs::remove_file(relocated.join(staged.vendor_marks.file_name().unwrap())).unwrap();
    fs::remove_dir_all(relocated.join("modules")).unwrap();
    let context: Vec<String> = context
        .iter()
        .map(|arg| arg.replace(root, path(&relocated)))
        .collect();
    let projection = relocated.join("apply.json");
    let args: Vec<&str> = ["run"]
        .into_iter()
        .chain(context.iter().map(String::as_str))
        .chain([
            "--projection-root",
            path(&relocated),
            "--apply-projection",
            path(&projection),
        ])
        .collect();
    assert_ok(
        &command(&args),
        "core applies after relocation without authored YAML",
    );
    assert_source_file(
        &relocated.join("projected-out/app/main/values.js"),
        &["42"],
        &[],
    );
}
