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

/// Canonicalize a selector by round-tripping it through swc (parse → codegen),
/// so equality is checked on the AST shape, not on incidental text formatting
/// (indentation, line breaks, trailing commas).
fn normalize_selector(source: &str) -> String {
    js_ast::with_swc_globals(|| {
        let module =
            js_ast::parse_js_module_ast("<selector expectation>", source).unwrap_or_else(|err| {
                panic!("selector is not parseable JavaScript ({err}):\n{source}")
            });
        js_ast::emit_module_source(&module).expect("emit normalized selector")
    })
}

fn assert_selector_shape(
    case_name: &str,
    output: &SelectorOutput,
    expected: &SelectorOutputExpectation,
) {
    assert_eq!(
        normalize_selector(&output.match_source),
        normalize_selector(expected.expected_match),
        "{case_name}: selector for {:?}\n  got: {}\n want: {}",
        output.exports,
        output.match_source.trim(),
        expected.expected_match.trim()
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
    minimizes_class_body,
    fixture = "class_body",
    name = "class selector keeps only discriminating member body anchors",
    module = "app/widgets",
    bindings = [("SelectedWidget", "selectedWidget")],
    expected = "expected_match.js",
);

// A target function whose body is a many-arm `switch` among same-shaped
// sibling switches minimizes to `case CASE_REST:` holes around the single
// discriminating `case` literal — closing the over-pin gap the survey flagged
// for many-arm switches. Active: `hole_switch_cases` emits the run holes and
// the matcher proves unique resolution.
minimizer_expectation_case!(
    minimizes_switch_case_run,
    fixture = "switch_case_run",
    name = "many-arm switch keeps only the discriminating case literal",
    module = "app/routers",
    bindings = [("SelectedRouter", "selectedRouter")],
    expected = "expected_match.js",
);

// A target class among many same-shape sibling classes minimizes to CLASS_REST
// holes plus the one member run carrying the discriminating value anchor (the
// unique `accept:` string), holing the receiver and the non-discriminating
// properties — never emitting the full class body (cf. the real
// `infra/http/PlatformApiService.yaml` conversion, ~330 lines). Routed through
// the class read-off path.
minimizer_expectation_case!(
    minimizes_class_among_many_siblings,
    fixture = "class_among_many_siblings",
    name = "class among many siblings keeps only the discriminating member",
    module = "app/services",
    bindings = [("SelectedService", "selectedService")],
    expected = "expected_match.js",
);

// A large object literal that shares most keys with sibling objects minimizes
// to OBJECT_PROPS holes on both sides of the single discriminating key, so the
// selector survives key reordering. W3 routed the single-target object form
// through the read-off shape index + padded-OBJECT_PROPS renderer, replacing the
// old retention path that kept all ~50 keys (each held to ANYTHING).
minimizer_expectation_case!(
    minimizes_object_keys_over_pinned,
    fixture = "object_keys_over_pinned",
    name = "large object keeps only the discriminating key value",
    module = "app/labels",
    bindings = [("SelectedLabels", "selectedLabels")],
    expected = "expected_match.js",
);

// A subclass among many sibling subclasses of a shared base minimizes to the
// `extends` clause (superclass holed with ANYTHING) plus the single
// discriminating class field, with CLASS_REST holes absorbing every other
// member. The read-off prefers the field's semantic value literal
// (`kind = "uniqueDiscriminatorShape"`) over the equally-selective `area` method
// name, anchoring on the value that survives a rebuild rather than the member
// name (cf. large blocks of sibling subclass declarations kept whole in the real
// spec).
minimizer_expectation_case!(
    minimizes_sibling_subclass_hierarchy,
    fixture = "sibling_subclass_hierarchy",
    name = "subclass among siblings keeps only the discriminating field initializer",
    module = "app/shapes",
    bindings = [("SelectedShape", "selectedShape")],
    expected = "expected_match.js",
);

// Aspirational: a function whose body is a long run of sequential assignment
// statements should minimize to STMT_LIST holes on both sides of the single
// assignment whose right-hand side carries the discriminating literal. Today
// the minimizer finds no sparse statement-level selector and bails (skips
// rather than emitting a full-AST pin), so the flat sequence of writes is
// never reduced to the one anchored assignment that uniquely identifies the
// target among siblings sharing the same write-block shape.
minimizer_expectation_case!(
    #[ignore = "statement minimizer bails instead of STMT_LIST + discriminating assignment"]
    minimizes_sequential_assignment_block,
    fixture = "sequential_assignment_block",
    name = "sequential assignment block keeps only the discriminating assignment",
    module = "app/reducers",
    bindings = [("SelectedReducer", "selectedReducer")],
    expected = "expected_match.js",
);

// Aspirational: a binding initialized by a deeply nested call tree should
// minimize to ANYTHING-holed outer callees and ARGS holes for their sibling
// arguments, drilling only to the one deep object literal that carries the
// discriminating key. Today the minimizer keeps the entire nested call/object
// tree whole, over-pinning every wrapper call and every transient argument
// instead of holing the path down to the single discriminating leaf.
minimizer_expectation_case!(
    #[ignore = "nested-call minimizer keeps whole tree instead of holing wrappers down to the leaf"]
    minimizes_deeply_nested_call_args,
    fixture = "deeply_nested_call_args",
    name = "deeply nested call tree keeps only the discriminating leaf literal",
    module = "app/views",
    bindings = [("SelectedView", "selectedView")],
    expected = "expected_match.js",
);

// Aspirational: a target object inside a multi-declarator group of sibling
// enum/lookup objects should minimize to DECLARATORS holes around the target
// declarator plus OBJECT_PROPS holes on both sides of the single discriminating
// key. Today the minimizer keeps every sibling declarator's full object value
// and every key of the target object, over-pinning a whole group of lookup
// dicts where one declarator with one anchored key would resolve uniquely.
minimizer_expectation_case!(
    #[ignore = "group minimizer keeps all declarators and all keys instead of DECLARATORS + OBJECT_PROPS"]
    minimizes_grouped_enum_objects,
    fixture = "grouped_enum_objects",
    name = "grouped enum objects keep only the target declarator's discriminating key",
    module = "app/palettes",
    bindings = [("SelectedPalette", "selectedPalette")],
    expected = "expected_match.js",
);

// Aspirational: a large lookup dictionary whose VALUES are nested objects
// (e.g. an id -> config map) should minimize to OBJECT_PROPS holes on both
// sides of the one entry whose nested value carries the discriminating
// literal, and that entry's own nested object should itself collapse to the
// discriminating property plus an OBJECT_PROPS hole. This generalizes
// `object_keys_over_pinned` (whose entry values are scalar string literals) to
// the common nested-object-value case. Today the minimizer keeps every entry's
// full nested object whole, over-pinning the entire dictionary where one
// anchored nested property would resolve uniquely. Mirrors the real spec's
// id->config maps (feature-flag / registry dicts) kept whole across ~50+
// nested-object entries.
minimizer_expectation_case!(
    #[ignore = "nested-value-dict minimizer keeps all entries' full nested objects instead of OBJECT_PROPS around one anchored nested property"]
    minimizes_object_nested_value_dict,
    fixture = "object_nested_value_dict",
    name = "nested-object-value dictionary keeps only the discriminating nested property",
    module = "app/registries",
    bindings = [("SelectedRegistry", "selectedRegistry")],
    expected = "expected_match.js",
);

// An object that carries a very long string / template-literal value (shared
// verbatim across siblings, so non-discriminating) alongside shorter unique
// features must never anchor on the long value. The cost tiebreak ranks equally
// selective+stable anchors by retained-source length, so the minimizer picks
// the *shortest* discriminator and holes everything else with OBJECT_PROPS. Here
// the shortest unique feature is `rank: 3` (siblings are rank 1/2), which beats
// the longer-but-also-unique `id: "uniqueDiscriminatorId"`. Without this, the
// long shared `prose` value (the largest node) would dominate the kept shape and
// produce rebuild-fragile, hundreds-of-lines selectors in the real spec
// (tool/command definitions and feature-flag tables whose `description`/`prose`
// template literals run for hundreds of lines).
minimizer_expectation_case!(
    minimizes_long_literal_value_anchor,
    fixture = "long_literal_value_anchor",
    name =
        "object anchors on the shortest discriminating feature, not the long shared literal value",
    module = "app/definitions",
    bindings = [("SelectedDefinition", "selectedDefinition")],
    expected = "expected_match.js",
);

// Aspirational: a function (commonly a UI component) whose body opens with a
// wide object-destructuring binding (`const { a, b, c, ... } = props;`) should
// minimize to OBJECT_PROPS holes around the single destructured property that
// discriminates this target from its siblings, plus STMT_LIST holes for the
// rest of the body. Today the minimizer keeps the entire wide destructuring
// pattern whole — every destructured name — even when one anchored property
// (and one body statement) would resolve uniquely, because the destructure
// block is a single large node it retains intact rather than holing its
// property run. Mirrors the real spec's React components whose 10-25-name
// `{ ... } = e` prop destructure is kept verbatim at the top of an otherwise
// holed body.
minimizer_expectation_case!(
    #[ignore = "wide-destructure minimizer keeps the entire destructuring pattern instead of OBJECT_PROPS around the one discriminating property"]
    minimizes_wide_destructure_block,
    fixture = "wide_destructure_block",
    name = "wide destructuring block keeps only the discriminating destructured property",
    module = "app/components",
    bindings = [("SelectedComponent", "selectedComponent")],
    expected = "expected_match.js",
);

// Aspirational: a SINGLE-target class with no same-shape sibling to discriminate
// against should still hole its body down to a few stable anchors (a unique
// member name plus a discriminating literal), absorbing every other member and
// the constructor with CLASS_REST and holing the kept member's body with
// STMT_LIST. Today, with no sibling to read off against, the minimizer finds no
// sparse selector and keeps the ENTIRE class body verbatim (zero holes) — the
// single most common and largest over-pin in the real survey (cf.
// `domains/search/live_search/ComputedViewRunner.yaml`, ~900 lines kept whole,
// and ~360 other fully-verbatim conversions). A whole-body pin is only
// marginally better than the original minified-name pin: alpha-matched and
// fails loudly, but rebuild-fragile and huge.
minimizer_expectation_case!(
    #[ignore = "single-target class with no sibling keeps the whole body verbatim instead of CLASS_REST + one anchored member"]
    minimizes_single_target_class_whole_body,
    fixture = "single_target_class_whole_body",
    name = "single-target class keeps only one discriminating member, not the whole body",
    module = "app/runners",
    bindings = [("SelectedRunner", "selectedRunner")],
    expected = "expected_match.js",
);

// Aspirational: a single-target React-style component (a function bound to a
// const, wrapped in a HOC call) whose body opens with a wide prop-destructure
// and continues with many hooks/handlers should minimize to a holed wrapper
// call (ANYTHING), a STMT_LIST-holed body, and a single anchored leaf — here the
// returned element's discriminating `className` literal — with OBJECT_PROPS
// absorbing the other element props. Today the minimizer keeps the whole
// component verbatim: the entire wide destructure pattern AND every body
// statement (cf. `features/nodes/cardView.yaml` `NodeAsCard`, ~1340 lines kept
// whole with only the outer declarator/wrapper holed). This is the
// whole-function-body analogue of `wide_destructure_block`, which isolates only
// the destructure-pattern holing on an already-sparse body.
minimizer_expectation_case!(
    #[ignore = "single-target component keeps the whole function body (wide destructure + every statement) instead of STMT_LIST down to the discriminating element literal"]
    minimizes_component_wide_destructure_whole_body,
    fixture = "component_wide_destructure_whole_body",
    name = "single-target component keeps only the discriminating returned-element literal, not the whole body",
    module = "app/components",
    bindings = [("SelectedComponent", "selectedComponent")],
    expected = "expected_match.js",
);

// Re-baselined for the unified keep-shallow anchor policy: both outputs keep each
// slot's direct shallow literals including object-property values. The group's
// shared `enabled: true` and the standalone's `kind: "panel"` / `title: "Settings"`
// / call arg `"settings"` are over-pinned versus the old `ANYTHING` holes. The
// design note accepts this occasional over-pin as the price of one policy across
// single (N=1 group) and multi-target group paths.
#[test]
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
