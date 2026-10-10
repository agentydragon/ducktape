//! Golden minimizer outputs: each fixture's `expected_match.js` is the selector
//! `synthesize-selectors --apply` emits for its `source.js`.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use debundle_e2e_support::{
    parse_stdout_json, run_debundle, run_match_selector, run_synthesize_selectors, write_text_file,
};

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

fn write_case(root: &Path, case: &MinimizedSelectorCase) -> (PathBuf, PathBuf) {
    let source = root.join("chunks/app.js");
    write_text_file(&source, case.source);

    let modules = root.join("modules");
    let mut module_yaml = String::from("members:\n");
    for binding in case.bindings {
        module_yaml.push_str(&format!(
            "  - name: {}\n    selector:\n      binding:\n        name: {}\n",
            binding.export_name, binding.runtime_name
        ));
    }
    write_text_file(&modules.join(format!("{}.yaml", case.module)), &module_yaml);
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
    // These golden selectors contain actual run holes, not keyword-like text
    // in literals. Reporting must include the holes the renderer emitted.
    for candidate in parsed["candidates"].as_array().unwrap() {
        let Some(source) = candidate["match_source"].as_str() else {
            continue;
        };
        for keyword in ["ARGS", "CASE_REST", "SEQ_EXPRS"] {
            if source.contains(keyword) {
                assert!(
                    candidate["rewritten_holes"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|hole| hole.as_str() == Some(keyword)),
                    "{}: missing {keyword} in {candidate}",
                    case.name,
                );
            }
        }
    }
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
    if let Some(source_matches) = doc["source_matches"].as_sequence() {
        for claim in source_matches {
            let Some(match_source) = claim["match"].as_str() else {
                continue;
            };
            let exports = source_match_binding_names(&claim["bindings"]);
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

fn source_match_binding_names(value: &serde_yaml::Value) -> BTreeSet<String> {
    let Some(bindings) = value.as_sequence() else {
        return BTreeSet::new();
    };
    bindings
        .iter()
        .filter_map(|binding| match binding {
            serde_yaml::Value::String(local) => Some(local.to_string()),
            serde_yaml::Value::Mapping(mapping) => mapping
                .get(serde_yaml::Value::String("name".to_string()))
                .or_else(|| mapping.get(serde_yaml::Value::String("local".to_string())))
                .and_then(serde_yaml::Value::as_str)
                .map(str::to_string),
            _ => None,
        })
        .collect()
}

/// Canonicalize a selector by round-tripping it through swc (parse → codegen),
/// so equality is checked on the AST shape, not on incidental text formatting
/// (indentation, line breaks, trailing commas).
fn normalize_selector(source: &str) -> String {
    js_ast::with_swc_globals(|| {
        let mut module = js_ast::parse_js_module_ast("<selector expectation>", source)
            .unwrap_or_else(|err| {
                panic!("selector is not parseable JavaScript ({err}):\n{source}")
            });
        // Compare paren-insensitively: the renderer drops redundant parens, prettier
        // re-adds them to the expected fixture, and the matcher itself sees through
        // parens — so canonicalize both sides before comparing.
        js_ast::strip_parens(&mut module);
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

/// Binary operators are deliberately absent from the read-off feature index.
/// An exact own-declaration selector still separates these siblings, so the
/// minimizer must find and relax that witness instead of leaving a name pin.
#[test]
fn relaxes_exact_declaration_when_read_off_cannot_see_operator() {
    for (name, source_text) in [
        (
            "function",
            "function a(x) { return x + 1; }\nfunction b(x) { return x * 1; }\n",
        ),
        (
            "class",
            "class a { value(x) { return x + 1; } }\nclass b { value(x) { return x * 1; } }\n",
        ),
        ("var", "const a = (x) => x + 1;\nconst b = (x) => x * 1;\n"),
        (
            "destructured var",
            "const { value: a } = foo(1 + 2);\nconst { value: b } = foo(1 * 2);\n",
        ),
        (
            "function repeated body",
            "function a(x) { x = x + 1; x = x + 1; x = x + 1; return x + 1; }\nfunction b(x) { x = x + 1; x = x + 1; x = x + 1; return x * 1; }\n",
        ),
        (
            "source identifier named like a hole",
            "function a(x) { return x + x; }\nfunction b(x) { return ARGS * x; }\n",
        ),
        (
            "source property named like a hole",
            "function a(x) { return x.foo + x; }\nfunction b(x) { return x.ARGS * x; }\n",
        ),
        (
            "object key named like a hole",
            "const a = { foo: 1, value: 1 + 1 };\nconst b = { ARGS: 1, value: 1 * 1 };\n",
        ),
        (
            "pattern key named like a hole",
            "function a({ foo: x, value: y }) { return y + x; }\nfunction b({ ARGS: x, value: y }) { return y * x; }\n",
        ),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let case = MinimizedSelectorCase {
            name,
            source: source_text,
            module: "app/target",
            bindings: &[BindingCase {
                export_name: "Selected",
                runtime_name: "b",
            }],
            outputs: &[],
        };
        let (modules, source) = write_case(dir.path(), &case);
        let out = run_synthesize_selectors(
            &modules,
            &[
                "--source-file",
                source.to_str().unwrap(),
                "--item",
                "app/target:Selected",
                "--format",
                "json",
            ],
        );
        let report = parse_stdout_json(&out);
        let selector = report["candidates"][0]["match_source"]
            .as_str()
            .unwrap_or_else(|| panic!("{name}: exact AST witness was skipped: {report}"));
        assert!(
            selector.contains('*'),
            "{name}: operator was lost: {selector}"
        );
        if name == "source identifier named like a hole" {
            assert!(!selector.contains("ARGS"), "{selector}");
        } else {
            assert!(
                selector.contains("ANYTHING") || selector.contains("STMT_LIST"),
                "{name}: exact declaration was not relaxed: {selector}"
            );
        }
        if name == "function repeated body" {
            assert_eq!(selector.matches("STMT_LIST").count(), 1, "{selector}");
        }
        let probe = run_match_selector(
            &source,
            selector,
            &["--target-binding", "Selected", "--no-slack"],
        );
        assert_eq!(probe["outcomes"][0]["outcome"]["binding"], "b", "{name}");
    }
}

/// The declarations have identical bodies. Their parameter property keys are
/// the only stable feature that distinguishes them; the local names can change.
#[test]
fn minimizes_function_using_a_destructured_parameter_key() {
    let dir = tempfile::tempdir().unwrap();
    let case = MinimizedSelectorCase {
        name: "destructured parameter key",
        source: "function a({ value: x }) { return x; }\nfunction b({ mode: y }) { return y; }\n",
        module: "app/functions",
        bindings: &[BindingCase {
            export_name: "Selected",
            runtime_name: "b",
        }],
        outputs: &[],
    };
    let (modules, source) = write_case(dir.path(), &case);
    let control = run_match_selector(
        &source,
        "function Selected({ mode: ANYTHING }) { STMT_LIST; }",
        &["--target-binding", "Selected", "--no-slack"],
    );
    assert_eq!(control["outcomes"][0]["outcome"]["kind"], "resolved");

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/functions:Selected",
            "--candidates",
            "10",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    let selector = parsed["candidates"][0]["match_source"]
        .as_str()
        .unwrap_or_else(|| panic!("no selector: {parsed}"));
    assert!(
        selector.contains("mode"),
        "the minimizer should consider the parameter key: {parsed}"
    );
    let variant = dir.path().join("renamed.js");
    write_text_file(
        &variant,
        "function c({ value: x }) { return x; }\nfunction d() {}\nfunction e({ mode: z }) { return z; }\n",
    );
    let renamed = run_match_selector(
        &variant,
        selector,
        &["--target-binding", "Selected", "--no-slack"],
    );
    assert_eq!(renamed["outcomes"][0]["outcome"]["binding"], "e");
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

// A scaffold-resolvable function still keeps its discriminating value anchor: the bare
// scaffold pins nothing rebuild-stable.
minimizer_expectation_case!(
    minimizes_robustness_value_over_scaffold,
    fixture = "robustness_value_over_scaffold",
    name = "scaffold-resolvable function still keeps a discriminating value anchor",
    module = "app/only",
    bindings = [("SelectedOnly", "selectedOnly")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_function_valued_init_holing,
    fixture = "function_valued_init_holing",
    name = "function-valued var init holes the callback body around the anchor",
    module = "app/handlers",
    bindings = [("SelectedHandler", "selectedHandler")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_object_property_literals,
    fixture = "object_property_literals",
    name = "object-in-call var keeps only the uniquely-present key",
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
    minimizes_binding_group_key_set_readoff,
    fixture = "binding_group_key_set_readoff",
    name = "multi-target group reads off each slot's discriminating key",
    module = "app/badges",
    bindings = [
        ("ErrorBadge", "errorBadge"),
        ("WarningBadge", "warningBadge")
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

minimizer_expectation_case!(
    minimizes_switch_case_run,
    fixture = "switch_case_run",
    name = "many-arm switch keeps only the discriminating case literal",
    module = "app/routers",
    bindings = [("SelectedRouter", "selectedRouter")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_class_among_many_siblings,
    fixture = "class_among_many_siblings",
    name = "class among many siblings keeps only the discriminating member",
    module = "app/services",
    bindings = [("SelectedService", "selectedService")],
    expected = "expected_match.js",
);

// A large object keeps the single discriminating key between `ANYTHING` holes, so the
// selector survives key reordering.
minimizer_expectation_case!(
    minimizes_object_keys_over_pinned,
    fixture = "object_keys_over_pinned",
    name = "large object keeps only the discriminating key value",
    module = "app/labels",
    bindings = [("SelectedLabels", "selectedLabels")],
    expected = "expected_match.js",
);

// A subclass among siblings keeps the holed `extends` clause and the field whose value
// literal discriminates it, preferred over an equally selective method name.
minimizer_expectation_case!(
    minimizes_sibling_subclass_hierarchy,
    fixture = "sibling_subclass_hierarchy",
    name = "subclass among siblings keeps only the discriminating field initializer",
    module = "app/shapes",
    bindings = [("SelectedShape", "selectedShape")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_sequential_assignment_block,
    fixture = "sequential_assignment_block",
    name = "sequential assignment block keeps only the discriminating assignment",
    module = "app/reducers",
    bindings = [("SelectedReducer", "selectedReducer")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_deeply_nested_call_args,
    fixture = "deeply_nested_call_args",
    name = "deeply nested call tree keeps only the discriminating leaf literal",
    module = "app/views",
    bindings = [("SelectedView", "selectedView")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_grouped_enum_objects,
    fixture = "grouped_enum_objects",
    name = "grouped enum objects keep only the target declarator's discriminating key",
    module = "app/palettes",
    bindings = [("SelectedPalette", "selectedPalette")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_grouped_sequence_enum,
    fixture = "grouped_sequence_enum",
    name = "grouped enum initializers use sequence run holes around stable assignments",
    module = "app/enums",
    bindings = [("First", "first"), ("Second", "second")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_grouped_computed_enum,
    fixture = "grouped_computed_enum",
    name = "computed enum assignments retain their arrow parameter binding",
    module = "app/enums",
    bindings = [("Left", "left"), ("Right", "right")],
    expected = "expected_match.js",
);

#[test]
fn computed_enum_selector_preserves_parameter_identity_across_renames() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source.js");
    write_text_file(
        &source,
        include_str!("testdata/selector_minimizer_expectations/grouped_computed_enum/source.js"),
    );
    let modules = dir.path().join("modules");
    write_text_file(
        &modules.join("app/enums.yaml"),
        "members:\n  - name: Left\n    selector:\n      binding: { name: left }\n  - name: Right\n    selector:\n      binding: { name: right }\n",
    );
    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/enums:Left",
            "--item",
            "app/enums:Right",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    let selector = parsed["candidates"][0]["match_source"]
        .as_str()
        .expect("grouped computed enum has a selector");
    for (fixture, expected) in [
        ("renamed.js", "resolved"),
        ("different_receiver.js", "no_match"),
    ] {
        let variant = dir.path().join(fixture);
        write_text_file(
            &variant,
            match fixture {
                "renamed.js" => include_str!(
                    "testdata/selector_minimizer_expectations/grouped_computed_enum/renamed.js"
                ),
                _ => include_str!(
                    "testdata/selector_minimizer_expectations/grouped_computed_enum/different_receiver.js"
                ),
            },
        );
        let result = run_match_selector(
            &variant,
            selector,
            &["--target-binding", "Right", "--no-slack"],
        );
        assert_eq!(
            result["outcomes"][0]["outcome"]["kind"], expected,
            "{fixture}: {selector}"
        );
    }
}

#[test]
fn function_and_method_selectors_preserve_parameter_identity() {
    for (kind, source, renamed, different_receiver) in [
        (
            "function",
            "var left = function(n) { return n[(n.A = 0)] = 'A'; };\nvar right = function(n) { return n[(n.B = 0)] = 'B'; };\n",
            "var left = function(n) { return n[(n.A = 0)] = 'A'; };\nvar right = function(q) { return q[(q.B = 0)] = 'B'; };\n",
            "const z = {};\nvar left = function(n) { return n[(n.A = 0)] = 'A'; };\nvar right = function(q) { return z[(z.B = 0)] = 'B'; };\n",
        ),
        (
            "method",
            "class Left { value(n) { return n[(n.A = 0)] = 'A'; } }\nclass Right { value(n) { return n[(n.B = 0)] = 'B'; } }\n",
            "class Left { value(n) { return n[(n.A = 0)] = 'A'; } }\nclass Right { value(q) { return q[(q.B = 0)] = 'B'; } }\n",
            "const z = {};\nclass Left { value(n) { return n[(n.A = 0)] = 'A'; } }\nclass Right { value(q) { return z[(z.B = 0)] = 'B'; } }\n",
        ),
        (
            "constructor",
            "class Left { constructor(n) { this.value = n[(n.A = 0)] = 'A'; } }\nclass Right { constructor(n) { this.value = n[(n.B = 0)] = 'B'; } }\n",
            "class Left { constructor(n) { this.value = n[(n.A = 0)] = 'A'; } }\nclass Right { constructor(q) { this.value = q[(q.B = 0)] = 'B'; } }\n",
            "const z = {};\nclass Left { constructor(n) { this.value = n[(n.A = 0)] = 'A'; } }\nclass Right { constructor(q) { this.value = z[(z.B = 0)] = 'B'; } }\n",
        ),
        (
            "destructured function",
            "var left = function({n}) { return n[(n.A = 0)] = 'A'; };\nvar right = function({n}) { return n[(n.B = 0)] = 'B'; };\n",
            "var left = function({n}) { return n[(n.A = 0)] = 'A'; };\nvar right = function({n:q}) { return q[(q.B = 0)] = 'B'; };\n",
            "const z = {};\nvar left = function({n}) { return n[(n.A = 0)] = 'A'; };\nvar right = function({n:q}) { return z[(z.B = 0)] = 'B'; };\n",
        ),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let source_file = dir.path().join("source.js");
        write_text_file(&source_file, source);
        let modules = dir.path().join("modules");
        let runtime_name = if kind.contains("function") {
            "right"
        } else {
            "Right"
        };
        write_text_file(
            &modules.join("app/case.yaml"),
            &format!(
                "members:\n  - name: Right\n    selector:\n      binding: {{ name: {runtime_name} }}\n"
            ),
        );
        let out = run_synthesize_selectors(
            &modules,
            &[
                "--source-file",
                source_file.to_str().unwrap(),
                "--item",
                "app/case:Right",
                "--format",
                "json",
            ],
        );
        let parsed = parse_stdout_json(&out);
        let selector = parsed["candidates"][0]["match_source"]
            .as_str()
            .unwrap_or_else(|| panic!("{kind}: no generated selector: {parsed}"));
        if kind == "destructured function" {
            assert!(selector.contains("({ n })"), "{kind}: {selector}");
        } else {
            assert!(selector.contains("(n)"), "{kind}: {selector}");
        }
        // A renamed destructure local keeps its property key (`{n:q}`), while
        // changing that key (`{q}`) changes the selector's meaning.
        for (variant, expected) in [(renamed, "resolved"), (different_receiver, "no_match")] {
            let variant_file = dir.path().join("variant.js");
            write_text_file(&variant_file, variant);
            let result = run_match_selector(
                &variant_file,
                selector,
                &["--target-binding", "Right", "--no-slack"],
            );
            assert_eq!(
                result["outcomes"][0]["outcome"]["kind"], expected,
                "{kind}: {selector}"
            );
        }
    }
}

minimizer_expectation_case!(
    minimizes_object_key_set_group,
    fixture = "object_key_set_group",
    name = "key-set object in a declarator group keeps only the discriminating key",
    module = "app/styles",
    bindings = [("ErrorPanelStyles", "errorPanelStyles")],
    expected = "expected_match.js",
);

// No single key is unique: the minimal discriminating key pair is kept, each between
// `ANYTHING` holes so key order stays free.
minimizer_expectation_case!(
    minimizes_object_key_set_subset,
    fixture = "object_key_set_subset",
    name = "key-set object keeps the minimal discriminating key subset",
    module = "app/shapes",
    bindings = [("TargetShape", "targetShape")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_object_nested_value_dict,
    fixture = "object_nested_value_dict",
    name = "nested-object-value dictionary keeps only the discriminating nested property",
    module = "app/registries",
    bindings = [("SelectedRegistry", "selectedRegistry")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_long_literal_value_anchor,
    fixture = "long_literal_value_anchor",
    name =
        "object anchors on the shortest discriminating feature, not the long shared literal value",
    module = "app/definitions",
    bindings = [("SelectedDefinition", "selectedDefinition")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_wide_destructure_block,
    fixture = "wide_destructure_block",
    name = "wide destructuring block keeps only the discriminating destructured property",
    module = "app/components",
    bindings = [("SelectedComponent", "selectedComponent")],
    expected = "expected_match.js",
);

// A single-target class keeps the cheapest single-occurrence value anchor (the
// multi-occurrence `0` is deferred) and holes the rest.
minimizer_expectation_case!(
    minimizes_single_target_class_whole_body,
    fixture = "single_target_class_whole_body",
    name = "single-target class keeps only one discriminating member, not the whole body",
    module = "app/runners",
    bindings = [("SelectedRunner", "selectedRunner")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_component_wide_destructure_whole_body,
    fixture = "component_wide_destructure_whole_body",
    name = "single-target component keeps only the discriminating returned-element literal, not the whole body",
    module = "app/components",
    bindings = [("SelectedComponent", "selectedComponent")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_interior_object_arg_holing,
    fixture = "interior_object_arg_holing",
    name = "nested object in a kept call arg keeps only the discriminating property, run holes for the rest",
    module = "app/nodes",
    bindings = [("SelectedMover", "selectedMover")],
    expected = "expected_match.js",
);

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

#[test]
fn minimizes_adjacent_accessor_group() {
    run_case(&MinimizedSelectorCase {
        name: "adjacent near-identical accessor functions collapse into one binding_group",
        source: include_str!(
            "testdata/selector_minimizer_expectations/adjacent_accessor_group/source.js"
        ),
        module: "app/accessors",
        bindings: &[
            BindingCase {
                export_name: "selectedAlphaAccessor",
                runtime_name: "selectedAlphaAccessor",
            },
            BindingCase {
                export_name: "selectedBetaAccessor",
                runtime_name: "selectedBetaAccessor",
            },
            BindingCase {
                export_name: "selectedGammaAccessor",
                runtime_name: "selectedGammaAccessor",
            },
            BindingCase {
                export_name: "selectedDeltaAccessor",
                runtime_name: "selectedDeltaAccessor",
            },
        ],
        outputs: &[SelectorOutputExpectation {
            exports: &[
                "selectedAlphaAccessor",
                "selectedBetaAccessor",
                "selectedGammaAccessor",
                "selectedDeltaAccessor",
            ],
            expected_match: include_str!(
                "testdata/selector_minimizer_expectations/adjacent_accessor_group/expected_group_match.js"
            ),
        }],
    });
}

#[test]
fn minimizes_sibling_class_declaration_group() {
    run_case(&MinimizedSelectorCase {
        name: "adjacent sibling class declarations collapse into one binding_group",
        source: include_str!(
            "testdata/selector_minimizer_expectations/sibling_class_declaration_group/source.js"
        ),
        module: "app/cards",
        bindings: &[
            BindingCase {
                export_name: "selectedAlphaCard",
                runtime_name: "selectedAlphaCard",
            },
            BindingCase {
                export_name: "selectedBetaCard",
                runtime_name: "selectedBetaCard",
            },
            BindingCase {
                export_name: "selectedGammaCard",
                runtime_name: "selectedGammaCard",
            },
            BindingCase {
                export_name: "selectedDeltaCard",
                runtime_name: "selectedDeltaCard",
            },
        ],
        outputs: &[SelectorOutputExpectation {
            exports: &[
                "selectedAlphaCard",
                "selectedBetaCard",
                "selectedGammaCard",
                "selectedDeltaCard",
            ],
            expected_match: include_str!(
                "testdata/selector_minimizer_expectations/sibling_class_declaration_group/expected_group_match.js"
            ),
        }],
    });
}

// An alpha-only `new` construct is anchored to its stable preceding call (`target_binding`
// at needle index 1).
minimizer_expectation_case!(
    anchors_alpha_only_construct_to_a_stable_neighbor,
    fixture = "neighbor_context_alpha_construct",
    name = "alpha-only `new` construct is anchored to its stable adjacent call",
    module = "app/helpers",
    bindings = [("SelectedHelper", "selectedHelper")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    anchors_duplicate_helper_to_a_stable_neighbor,
    fixture = "neighbor_context_duplicate_helper",
    name = "near-duplicate helper function is anchored to its stable adjacent call",
    module = "app/helpers",
    bindings = [("SelectedHelper", "selectedHelper")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_neighbor_context_whole_function_neighbor,
    fixture = "neighbor_context_whole_function_neighbor",
    name = "neighbor function declaration is holed to its discriminating anchor, not pinned whole",
    module = "app/helpers",
    bindings = [("SelectedHelper", "selectedHelper")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_class_expression_const_whole_body,
    fixture = "class_expression_const_whole_body",
    name = "class-expression const keeps only one discriminating member, not the whole class body",
    module = "app/stores",
    bindings = [("SelectedStore", "selectedStore")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_class_sequence_constructor_body,
    fixture = "class_sequence_constructor_body",
    name = "class anchor inside a constructor sequence expression is holed in place, not pinned via a neighbor",
    module = "app/errors",
    bindings = [("SelectedError", "selectedError")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_jointly_unique_binding_group,
    fixture = "jointly_unique_binding_group",
    name = "ambiguous individual slots resolve as a unique tuple",
    module = "app/pair",
    bindings = [("SelectedLeft", "left"), ("SelectedRight", "right")],
    expected = "expected_match.js",
);

#[test]
fn falls_back_to_single_declarators_when_group_references_a_member() {
    let case = MinimizedSelectorCase {
        name: "group with a reference to another member",
        source: "const noise = buildMenu(),\n  empty = values => Object.values(values).every(value => value === void 0),\n  spacer = 2,\n  entry = node => ({ id: node.id, direction: \"ASC\" }),\n  entries = node => (node?.children.map(entry) || []).sort(),\n  tail = 3;\nexport { empty, entry, entries };\n",
        module: "app/helpers",
        bindings: &[
            BindingCase {
                export_name: "Empty",
                runtime_name: "empty",
            },
            BindingCase {
                export_name: "Entry",
                runtime_name: "entry",
            },
            BindingCase {
                export_name: "Entries",
                runtime_name: "entries",
            },
        ],
        outputs: &[],
    };
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = write_case(dir.path(), &case);
    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--module",
            "app/helpers",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["summary"]["changed_candidates"], 3, "{parsed}");
    assert_eq!(parsed["summary"]["skipped_candidates"], 0, "{parsed}");
    let rewritten = fs::read_to_string(modules.join("app/helpers.yaml")).unwrap();
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
    let outputs = collect_selector_outputs(&doc);
    assert_eq!(outputs.len(), 3, "{outputs:?}");
    assert_eq!(
        outputs
            .into_iter()
            .map(|output| output.exports)
            .collect::<BTreeSet<_>>(),
        ["Empty", "Entry", "Entries"]
            .into_iter()
            .map(|name| BTreeSet::from([name.to_string()]))
            .collect()
    );
}

minimizer_expectation_case!(
    minimizes_neighbor_class_context,
    fixture = "neighbor_class_context",
    name = "neighbor class retains only its discriminating member",
    module = "app/helpers",
    bindings = [("SelectedHelper", "selectedHelper")],
    expected = "expected_match.js",
);

minimizer_expectation_case!(
    minimizes_binding_group_unindexed_literals,
    fixture = "binding_group_unindexed_literals",
    name = "tuple read-off retains null, regex and template literal coverage",
    module = "app/literals",
    bindings = [
        ("SelectedNull", "selectedNull"),
        ("SelectedRegex", "selectedRegex"),
        ("SelectedTemplate", "selectedTemplate"),
    ],
    expected = "expected_match.js",
);

/// Two classes are identical, but a property in a use site identifies one.
/// This needs a free-identifier binding claim rather than declaration context.
#[test]
fn minimizes_identical_class_using_a_named_use_site() {
    let dir = tempfile::tempdir().unwrap();
    let case = MinimizedSelectorCase {
        name: "identical classes with a named use site",
        source: "class a extends Error {}\nclass b extends Error {}\nconst roles = { primary: a, secondary: b };\n",
        module: "app/classes",
        bindings: &[BindingCase {
            export_name: "Selected",
            runtime_name: "a",
        }],
        outputs: &[],
    };
    let (modules, source) = write_case(dir.path(), &case);

    // Prove that the selector language can already identify this binding.
    write_text_file(
        &modules.join("app/classes.yaml"),
        "source_matches:\n  - match: 'const roles = { primary: Selected, secondary: ANYTHING };'\n    bindings:\n      - Selected\n",
    );
    let validated = run_debundle(&[
        "spec",
        "validate",
        "--modules",
        modules.to_str().unwrap(),
        "--source-file",
        source.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        validated.status.success(),
        "manual use-site control failed:\n{}\n{}",
        String::from_utf8_lossy(&validated.stdout),
        String::from_utf8_lossy(&validated.stderr)
    );

    write_case(dir.path(), &case);
    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/classes:Selected",
            "--candidates",
            "10",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    let selector = parsed["candidates"][0]["match_source"]
        .as_str()
        .unwrap_or_else(|| panic!("no selector: {parsed}"));
    assert!(
        selector.contains("primary"),
        "the minimizer should consider the named use site: {parsed}"
    );

    run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/classes:Selected",
            "--apply",
            "--format",
            "json",
        ],
    );
    let rewritten = fs::read_to_string(modules.join("app/classes.yaml")).unwrap();
    assert!(rewritten.contains("source_matches:"), "{rewritten}");

    let variant = dir.path().join("renamed.js");
    write_text_file(
        &variant,
        "class x extends Error {}\nclass y extends Error {}\nconst roles = { secondary: y, extra: 1, primary: x };\n",
    );
    let renamed = run_match_selector(
        &variant,
        selector,
        &["--target-binding", "Selected", "--no-slack"],
    );
    assert_eq!(renamed["outcomes"][0]["outcome"]["binding"], "x");
    let validated = run_debundle(&[
        "spec",
        "validate",
        "--modules",
        modules.to_str().unwrap(),
        "--source-file",
        variant.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        validated.status.success(),
        "generated use-site selector failed after renaming/reordering:\n{}\n{}",
        String::from_utf8_lossy(&validated.stdout),
        String::from_utf8_lossy(&validated.stderr)
    );
}
