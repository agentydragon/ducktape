//! Ignored expectation tests for the eventual full selector minimizer.
//!
//! These cases document target behavior, not the current implementation's
//! limits. Each fixture's expected match is an exact motivational target for
//! the selector shape we currently want. If the implementation later finds an
//! equivalently minimal or better shape, update that fixture to the actual
//! `f(input) = output` before unignoring the individual case.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

struct MinimizedSelectorCase {
    name: &'static str,
    source: &'static str,
    module: &'static str,
    bindings: &'static [BindingCase],
    outputs: &'static [SelectorOutputExpectation],
}

struct BindingCase {
    export_name: &'static str,
    runtime_name: &'static str,
}

struct SelectorOutputExpectation {
    exports: &'static [&'static str],
    expected_match: &'static str,
}

#[derive(Debug)]
struct SelectorOutput {
    exports: BTreeSet<String>,
    match_source: String,
}

fn debundle_binary() -> PathBuf {
    let runfiles_path = std::env::var("RUNFILES_DIR")
        .or_else(|_| std::env::var("TEST_SRCDIR"))
        .expect("runfiles env var");
    let candidate = Path::new(&runfiles_path).join("_main/devinfra/js/debundle/debundle");
    assert!(
        candidate.exists(),
        "debundle binary not at {}",
        candidate.display()
    );
    candidate
}

fn write(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, body).unwrap();
}

fn run_synthesize_selectors(modules: &Path, extra: &[&str]) -> std::process::Output {
    let mut args = vec![
        "spec",
        "synthesize-selectors",
        "--modules",
        modules.to_str().unwrap(),
    ];
    args.extend_from_slice(extra);
    let out = Command::new(debundle_binary())
        .args(&args)
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "non-zero exit\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    out
}

fn parse_stdout_json(out: &std::process::Output) -> Value {
    serde_json::from_slice(&out.stdout).unwrap_or_else(|err| {
        panic!(
            "stdout is not JSON ({err})\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr),
        )
    })
}

fn write_case(root: &Path, case: &MinimizedSelectorCase) -> (PathBuf, PathBuf) {
    let source = root.join("chunks/app.js");
    write(&source, case.source);

    let modules = root.join("modules");
    let mut module_yaml = String::from("members:\n");
    for binding in case.bindings {
        module_yaml.push_str(&format!(
            "  - name: {}\n    selector:\n      binding:\n        name: {}\n",
            binding.export_name, binding.runtime_name
        ));
    }
    write(&modules.join(format!("{}.yaml", case.module)), &module_yaml);
    (modules, source)
}

fn run_case(case: &MinimizedSelectorCase) {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = write_case(dir.path(), case);
    let mut args = vec![
        "--source-file".to_string(),
        source.to_str().unwrap().to_string(),
    ];
    for binding in case.bindings {
        args.push("--item".to_string());
        args.push(format!("{}:{}", case.module, binding.export_name));
    }
    args.extend([
        "--apply".to_string(),
        "--format".to_string(),
        "json".to_string(),
    ]);
    let arg_refs = args.iter().map(String::as_str).collect::<Vec<_>>();

    let out = run_synthesize_selectors(&modules, &arg_refs);
    let parsed = parse_stdout_json(&out);
    let changed = parsed["summary"]["changed_candidates"]
        .as_u64()
        .unwrap_or(0);
    assert!(
        changed > 0,
        "{}: expected a selector rewrite: {parsed}",
        case.name
    );

    let rewritten = fs::read_to_string(modules.join(format!("{}.yaml", case.module))).unwrap();
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
    let outputs = collect_selector_outputs(&doc);
    assert_eq!(
        outputs.len(),
        case.outputs.len(),
        "{}: expected selector output partition {:?}, got {:?}",
        case.name,
        case.outputs
            .iter()
            .map(|output| output.exports)
            .collect::<Vec<_>>(),
        outputs
    );
    for expected in case.outputs {
        let expected_exports = expected
            .exports
            .iter()
            .map(|export| (*export).to_string())
            .collect::<BTreeSet<_>>();
        let output = outputs
            .iter()
            .find(|output| output.exports == expected_exports)
            .unwrap_or_else(|| {
                panic!(
                    "{}: missing selector output for exports {:?}; got {:?}",
                    case.name, expected_exports, outputs
                )
            });
        assert_selector_shape(case.name, output, expected);
    }
}

fn collect_selector_outputs(doc: &serde_yaml::Value) -> Vec<SelectorOutput> {
    let mut outputs = Vec::new();
    if let Some(members) = doc["members"].as_sequence() {
        for member in members {
            let Some(match_source) = member["selector"]["source_match"]["match"].as_str() else {
                continue;
            };
            let Some(export_name) = member["name"].as_str() else {
                continue;
            };
            outputs.push(SelectorOutput {
                exports: BTreeSet::from([export_name.to_string()]),
                match_source: match_source.trim().to_string(),
            });
        }
    }
    if let Some(binding_groups) = doc["binding_groups"].as_sequence() {
        for group in binding_groups {
            let Some(match_source) = group["source_match"]["match"].as_str() else {
                continue;
            };
            let exports = mapping_string_keys(&group["exports"]);
            if exports.is_empty() {
                continue;
            }
            outputs.push(SelectorOutput {
                exports,
                match_source: match_source.trim().to_string(),
            });
        }
    }
    outputs
}

fn mapping_string_keys(value: &serde_yaml::Value) -> BTreeSet<String> {
    let serde_yaml::Value::Mapping(mapping) = value else {
        return BTreeSet::new();
    };
    mapping
        .keys()
        .filter_map(|key| key.as_str().map(str::to_string))
        .collect()
}

fn assert_selector_shape(
    case_name: &str,
    output: &SelectorOutput,
    expected: &SelectorOutputExpectation,
) {
    let match_source = output.match_source.trim();
    assert_eq!(
        match_source,
        expected.expected_match.trim(),
        "{case_name}: selector for {:?}",
        output.exports
    );
}

macro_rules! minimizer_expectation_case {
    (
        $(#[$attr:meta])*
        $test_name:ident,
        fixture = $fixture:literal,
        name = $case_name:literal,
        module = $module:literal,
        bindings = [$(($export_name:literal, $runtime_name:literal)),+ $(,)?],
        expected = $expected:literal $(,)?
    ) => {
        #[test]
        $(#[$attr])*
        fn $test_name() {
            run_case(&MinimizedSelectorCase {
                name: $case_name,
                source: include_str!(concat!(
                    "testdata/selector_minimizer_expectations/",
                    $fixture,
                    "/source.js"
                )),
                module: $module,
                bindings: &[$(BindingCase {
                    export_name: $export_name,
                    runtime_name: $runtime_name,
                }),+],
                outputs: &[SelectorOutputExpectation {
                    exports: &[$($export_name),+],
                    expected_match: include_str!(concat!(
                        "testdata/selector_minimizer_expectations/",
                        $fixture,
                        "/",
                        $expected
                    )),
                }],
            });
        }
    };
}

minimizer_expectation_case!(
    minimizes_sparse_function_body,
    fixture = "sparse_function_body",
    name = "sparse function body with two statement anchors",
    module = "app/workers",
    bindings = [("SelectedWorker", "selectedWorker")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_call_argument_literal,
    fixture = "call_argument_literal",
    name = "method call keeps only discriminating argument literal",
    module = "app/calls",
    bindings = [("SelectedCall", "selectedCall")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    #[ignore = "target behavior: object-literal multi-key retention (not yet implemented)"]
    minimizes_object_property_literals,
    fixture = "object_property_literals",
    name = "object literal keeps minimal key value anchors",
    module = "app/config",
    bindings = [("SelectedConfig", "selectedConfig")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_binding_group_declarators,
    fixture = "binding_group_declarators",
    name = "binding group keeps only target declarators and literal values",
    module = "app/limits",
    bindings = [
        ("SelectedLimit", "selectedLimit"),
        ("SelectedThreshold", "selectedThreshold"),
    ],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_nested_async_try,
    fixture = "nested_async_try",
    name = "nested async try block keeps only nested discriminating call",
    module = "app/loaders",
    bindings = [("SelectedLoader", "selectedLoader")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    #[ignore = "target behavior: class member-body descent (not yet implemented)"]
    minimizes_class_body,
    fixture = "class_body",
    name = "class selector keeps only discriminating member body anchors",
    module = "app/widgets",
    bindings = [("SelectedWidget", "selectedWidget")],
    expected = "expected_match.js",
);

#[test]
#[ignore = "target behavior for future selector partition planning"]
fn minimizes_binding_group_partition() {
    run_case(&MinimizedSelectorCase {
        name: "nearby targets become a binding group while distant targets stay individual",
        source: include_str!(
            "testdata/selector_minimizer_expectations/binding_group_partition/source.js"
        ),
        module: "app/partition",
        bindings: &[
            BindingCase {
                export_name: "SelectedPrimary",
                runtime_name: "selectedPrimary",
            },
            BindingCase {
                export_name: "SelectedSecondary",
                runtime_name: "selectedSecondary",
            },
            BindingCase {
                export_name: "SelectedStandalone",
                runtime_name: "selectedStandalone",
            },
        ],
        outputs: &[
            SelectorOutputExpectation {
                exports: &["SelectedPrimary", "SelectedSecondary"],
                expected_match: include_str!(
                    "testdata/selector_minimizer_expectations/binding_group_partition/expected_group_match.js"
                ),
            },
            SelectorOutputExpectation {
                exports: &["SelectedStandalone"],
                expected_match: include_str!(
                    "testdata/selector_minimizer_expectations/binding_group_partition/expected_standalone_match.js"
                ),
            },
        ],
    });
}
