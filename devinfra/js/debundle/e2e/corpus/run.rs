use super::case::Expectation;
use super::{Case, relative, strings};
use debundle_e2e_support::{
    CaseWorkspace, assert_all_emitted_js_checks, assert_module_exports, assert_module_source,
    assert_node_output, assert_preserved_entry_output, run_serialized_fixture,
    run_serialized_rejection_fixture, write_text_file, write_yaml_file,
};
use serde_yaml::Value;
use std::fs;
use std::path::Path;

pub(super) fn run(case: &Case) {
    // Keep source-root snapshots outside the parent's command journal.
    let workspace = CaseWorkspace::default().child();
    let root = workspace.path();
    for input in &case.inputs {
        relative(&input.relative);
        let target = root.join(&input.relative);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::copy(&input.source, target).expect("copy corpus input");
    }
    let manifest = case
        .inputs
        .iter()
        .find(|file| file.relative == Path::new("js-files.txt"));
    let listing = match manifest {
        Some(file) => fs::read_to_string(&file.source).expect("read explicit chunk list"),
        None => {
            let mut chunks: Vec<_> = case
                .inputs
                .iter()
                .filter(|file| {
                    file.relative
                        .extension()
                        .is_some_and(|extension| extension == "js")
                })
                .map(|file| file.relative.to_str().expect("UTF-8 chunk path"))
                .collect();
            chunks.sort_unstable();
            assert!(!chunks.is_empty(), "{} has no JavaScript input", case.name);
            chunks
                .into_iter()
                .map(|chunk| format!("{chunk}\n"))
                .collect()
        }
    };
    write_text_file(&root.join(".fixture/js-files.txt"), &listing);
    if !root.join("package.json").exists() {
        write_text_file(&root.join("package.json"), "{\"type\":\"module\"}\n");
    }
    relative(&case.spec);
    apply_defaults(&root.join(&case.spec));
    match &case.expectation {
        Expectation::Accept { accept: expected } => {
            let runtime_files: Vec<_> = expected
                .runtime_files
                .iter()
                .map(|(destination, source)| {
                    relative(Path::new(destination));
                    relative(source);
                    (
                        destination.as_str(),
                        fs::read_to_string(root.join(source)).expect("read runtime sibling"),
                    )
                })
                .collect();
            let borrowed: Vec<_> = runtime_files
                .iter()
                .map(|(path, source)| (*path, source.as_str()))
                .collect();
            let fixture =
                run_serialized_fixture(workspace.clone(), &case.spec, &expected.entry, &borrowed);
            assert_preserved_entry_output(&fixture, &expected.stdout);
            if expected.check_all_js {
                assert_all_emitted_js_checks(&fixture);
            }
            for (path, exports) in &expected.exports {
                relative(path);
                assert_module_exports(
                    &fixture.out_root,
                    path.to_str().unwrap(),
                    &strings(&exports.include),
                    &strings(&exports.exclude),
                );
            }
            for (path, source) in &expected.source {
                relative(path);
                assert_module_source(
                    &fixture.out_root,
                    path.to_str().unwrap(),
                    &strings(&source.contains),
                    &strings(&source.excludes),
                );
                if !source.occurrences_at_least.is_empty() {
                    let code = fs::read_to_string(fixture.out_root.join(path))
                        .expect("read emitted source");
                    for (needle, minimum) in &source.occurrences_at_least {
                        assert!(
                            code.matches(needle.as_str()).count() >= *minimum,
                            "{}: expected at least {minimum} occurrences of {needle:?}\n{code}",
                            path.display()
                        );
                    }
                }
            }
            for (probe, expected_probe) in &expected.probes {
                relative(probe);
                let probe_path = root.join(probe);
                for entry in [fixture.source_path.clone(), fixture.entry_path.clone()] {
                    let script = format!(
                        "const entry = await import({});\nconst {{ default: probe }} = await import({});\nawait probe(entry);\n",
                        serde_json::to_string(entry.to_str().unwrap()).unwrap(),
                        serde_json::to_string(probe_path.to_str().unwrap()).unwrap()
                    );
                    let script_path = root.join(".fixture/probe.mjs");
                    write_text_file(&script_path, &script);
                    assert_node_output(&script_path, &expected_probe.stdout, "");
                }
            }
        }
        Expectation::Reject { reject: expected } => {
            let fixture =
                run_serialized_rejection_fixture(workspace.clone(), &case.spec, &expected.entry);
            assert!(
                !expected.atomic_conflict.modules.is_empty(),
                "an atomic conflict must name its modules"
            );
            fixture.assert_atomic_conflict(
                &strings(&expected.atomic_conflict.modules),
                &strings(&expected.atomic_conflict.causes),
            );
        }
    }
}

fn apply_defaults(path: &Path) {
    let mut authored: serde_yaml::Mapping =
        serde_yaml::from_slice(&fs::read(path).expect("read authored spec"))
            .expect("parse authored spec");
    let defaults = [
        (
            "inputs",
            vec![
                ("input_root", Value::from(".")),
                ("js_list_path", Value::from(".fixture/js-files.txt")),
            ],
        ),
        (
            "materialize_logical_modules",
            vec![
                ("target_dir", Value::from("modules")),
                ("report_out_dir", Value::from("out/reports/tree")),
                ("prune_other_chunks", Value::from(false)),
            ],
        ),
        ("write_js_tree", vec![("out_dir", Value::from("out"))]),
    ];
    for (section, defaults) in defaults {
        let section = authored
            .entry(Value::from(section))
            .or_insert_with(|| Value::Mapping(serde_yaml::Mapping::new()));
        if let Value::Mapping(fields) = section {
            for (key, default) in defaults {
                fields.entry(Value::from(key)).or_insert(default);
            }
        }
    }
    // The CLI and retained replay inputs receive the same complete spec.
    write_yaml_file(path, &authored);
}
