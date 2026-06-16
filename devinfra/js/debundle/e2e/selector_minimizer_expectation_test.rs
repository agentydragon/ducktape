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

// Robustness-anchor policy: a function that the bare scaffold alone would resolve
// (it is the only arity-2 function in the chunk) still keeps its discriminating
// value anchor (`"uniqueRobustnessToken"`) rather than emitting the degenerate
// `function X(ANYTHING, ANYTHING) { STMT_LIST }`. The bare scaffold pins nothing
// rebuild-stable and matches any arity-2 function a rebuild adds, so the read-off
// prefers a holed-down value anchor when it has one.
minimizer_expectation_case!(
    minimizes_robustness_value_over_scaffold,
    fixture = "robustness_value_over_scaffold",
    name = "scaffold-resolvable function still keeps a discriminating value anchor",
    module = "app/only",
    bindings = [("SelectedOnly", "selectedOnly")],
    expected = "expected_match.js",
);

// A single-target var initialized by a call wrapping a function expression
// (`registerHandler(function (event) {…})`) drills into the function body via the
// `hole_expr` `Expr::Fn` arm: the params hole to `ANYTHING`, the body to
// `STMT_LIST` runs around the one statement carrying the discriminating string,
// instead of pinning the whole callback verbatim.
minimizer_expectation_case!(
    minimizes_function_valued_init_holing,
    fixture = "function_valued_init_holing",
    name = "function-valued var init holes the callback body around the anchor",
    module = "app/handlers",
    bindings = [("SelectedHandler", "selectedHandler")],
    expected = "expected_match.js",
);

// A single-target var initialized by an object-bearing call (`buildWidget({…})`)
// routes through the var read-off path (`try_var_read_off` → `hole_expr` →
// `hole_object`). The read-off picks the uniquely-present `onClick` key (held to
// `ANYTHING`) as the sole minimal discriminator — sparser than keeping the
// `kind`/`mode` value pair the siblings also carry.
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

// A function whose body is a long run of sequential assignment statements
// minimizes to STMT_LIST holes on both sides of the single assignment whose
// right-hand side carries the discriminating literal. The assignment's LHS
// receiver (`state`, a minified parameter) holes to `ANYTHING` while the stable
// property name and discriminating RHS literal are kept, so the flat sequence of
// writes reduces to the one anchored `ANYTHING.delta = "…"` assignment that
// uniquely identifies the target among siblings sharing the write-block shape.
minimizer_expectation_case!(
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
// discriminating key. The var read-off path now drills correctly to the leaf
// (`buildInner({ mode: "…", OBJECT_PROPS })`), closing the old "keeps the whole
// tree" gap. Two gaps remain vs the aspirational shape: the bare-function callees
// (`wrapOuter`/`decorate`/`buildInner`) stay pinned rather than holing to ANYTHING
// (the `hole_callee` policy keeps a bare function reference as a stable pin), and a
// dropped sibling arg holes to a single arity-exact `ANYTHING` rather than a
// variadic `ARGS` run-hole. Both are cross-cutting hole-policy changes.
minimizer_expectation_case!(
    #[ignore = "var read-off drills to the leaf but keeps bare-function callees (hole_callee policy) and holes dropped args to arity-exact ANYTHING, not ARGS"]
    minimizes_deeply_nested_call_args,
    fixture = "deeply_nested_call_args",
    name = "deeply nested call tree keeps only the discriminating leaf literal",
    module = "app/views",
    bindings = [("SelectedView", "selectedView")],
    expected = "expected_match.js",
);

// A target object inside a multi-declarator group of sibling enum/lookup objects
// minimizes to DECLARATORS holes around the target declarator plus OBJECT_PROPS
// holes on both sides of the single discriminating entry. The slot-aware
// `cover_object_slot` greedy pins the entry whose value is globally unique
// (`accent: "uniqueDiscriminatorAccent"`), resolving the binding to the right
// declarator slot without keeping every sibling declarator's full object or every
// key of the target object.
minimizer_expectation_case!(
    minimizes_grouped_enum_objects,
    fixture = "grouped_enum_objects",
    name = "grouped enum objects keep only the target declarator's discriminating key",
    module = "app/palettes",
    bindings = [("SelectedPalette", "selectedPalette")],
    expected = "expected_match.js",
);

// Key-set minimization inside a multi-declarator group (#2290): when an object is
// discriminated by its *key set* — every value already holed to ANYTHING (here
// `theme.base` member accesses shared across all siblings) — the minimizer keeps
// only the minimal discriminating key (`logViewer`, unique to the target slot)
// with OBJECT_PROPS holes for the rest, and DECLARATORS holes for the sibling
// declarators, instead of keeping every key of the target object. Mirrors the
// real gaffer CSS-styles dicts (`{ diagnosticsSection: …, detailsToggle: …, … }`)
// kept whole inside `DECLARATORS_BEFORE`/`_AFTER` groups.
minimizer_expectation_case!(
    minimizes_object_key_set_group,
    fixture = "object_key_set_group",
    name = "key-set object in a declarator group keeps only the discriminating key",
    module = "app/styles",
    bindings = [("ErrorPanelStyles", "errorPanelStyles")],
    expected = "expected_match.js",
);

// Key-set minimization needing a *subset* of keys (#2290): no single key is
// unique (each is shared with one sibling) but the pair `{ alpha, delta }`
// discriminates the target. With every value holed to ANYTHING, the minimizer
// keeps exactly those two non-adjacent keys, each surrounded by OBJECT_PROPS so
// the key set matches as independent interior elements (surviving key reorder),
// rather than keeping all four keys.
minimizer_expectation_case!(
    minimizes_object_key_set_subset,
    fixture = "object_key_set_subset",
    name = "key-set object keeps the minimal discriminating key subset",
    module = "app/shapes",
    bindings = [("TargetShape", "targetShape")],
    expected = "expected_match.js",
);

// A large lookup dictionary whose VALUES are nested objects (e.g. an id -> config
// map) minimizes to OBJECT_PROPS holes on both sides of the one entry whose nested
// value carries the discriminating literal, and that entry's own nested object
// itself collapses to the discriminating property plus OBJECT_PROPS. Generalizes
// `object_keys_over_pinned` (scalar entry values) to the nested-object-value case;
// handled by the read-off drilling through the nested object/array holing
// (`hole_object` / `hole_array` recursion). Mirrors the real spec's id->config
// maps (feature-flag / registry dicts) otherwise kept whole across ~50+ entries.
minimizer_expectation_case!(
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
    #[ignore = "the discriminator is a destructure-pattern property key (`uniqueDiscriminatorProp`), which the candidate index does not collect, and holing an `ObjectPat` (keep one property + OBJECT_PROPS) is not yet implemented; the read-off bails with no sparse selector. Needs destructure-pattern-key anchor indexing + pattern holing."]
    minimizes_wide_destructure_block,
    fixture = "wide_destructure_block",
    name = "wide destructuring block keeps only the discriminating destructured property",
    module = "app/components",
    bindings = [("SelectedComponent", "selectedComponent")],
    expected = "expected_match.js",
);

// A SINGLE-target class with no same-shape sibling holes its body down to one
// stable value anchor, absorbing every other member and the constructor with
// CLASS_REST and holing the kept member's body with STMT_LIST. The empty scaffold
// `class SelectedRunner { CLASS_REST; }` resolves uniquely (it is the only class)
// but pins nothing rebuild-stable (criterion 5); the robustness-anchor policy
// instead drills to a value anchor. The candidate index already collects literals
// inside method bodies, but the *minimal* anchor (`NumberLiteral("0")`, in class
// fields / a `void 0`) renders to a constructor the holer keeps verbatim, which
// fails to prove. The renderer then walks the target's individually-discriminating
// value anchors best-first (issue #2289 item 1) and lands on `"running"` inside
// `applyChange`, holing the receiver chain to `ANYTHING.set("running")` — sparser
// and more rebuild-robust than pinning the minified `this.boxedStatus` receiver.
// The real-survey whole-body over-pin
// (`domains/search/live_search/ComputedViewRunner.yaml`, ~900 lines kept whole;
// ~360 other fully-verbatim conversions) only manifests when same-shape SIBLING
// classes force body-content discrimination; this reduction pins the
// single-target degenerate-scaffold half of the gap.
minimizer_expectation_case!(
    minimizes_single_target_class_whole_body,
    fixture = "single_target_class_whole_body",
    name = "single-target class keeps only one discriminating member, not the whole body",
    module = "app/runners",
    bindings = [("SelectedRunner", "selectedRunner")],
    expected = "expected_match.js",
);

// A single-target React-style component (a function bound to a const, wrapped in
// a HOC call) whose body opens with a wide prop-destructure and continues with
// many hooks/handlers minimizes to a STMT_LIST-holed function body drilled down to
// the discriminating returned-element literal, rather than the degenerate
// `const SelectedComponent = ANYTHING`. The var read-off iterates the target's
// individually-discriminating value anchors best-first (issue #2289 item 1) and
// keeps `jsx("div", ANYTHING)` — anchoring on the unique `"div"` tag literal — with
// the prop destructure and hooks absorbed into `STMT_LIST`. The bare callees
// `wrap`/`jsx` stay pinned (the documented `hole_callee` policy keeps a bare
// function reference as a stable pin); holing them to `ANYTHING` is the
// cross-cutting `hole_callee` change tracked by `deeply_nested_call_args`. The real
// whole-body over-pin (cf. `features/nodes/cardView.yaml` `NodeAsCard`, ~1340 lines
// kept whole with only the outer declarator/wrapper holed) needs sibling-bearing
// fixtures to reproduce; this reduction pins the single-target degenerate-scaffold
// half.
minimizer_expectation_case!(
    minimizes_component_wide_destructure_whole_body,
    fixture = "component_wide_destructure_whole_body",
    name = "single-target component keeps only the discriminating returned-element literal, not the whole body",
    module = "app/components",
    bindings = [("SelectedComponent", "selectedComponent")],
    expected = "expected_match.js",
);

// Interior holing: a nested object literal inside a kept call argument should
// hole its non-anchor properties to `OBJECT_PROPS`, the same way the single-target
// object form already does at top level. The discriminator is one property
// (`mode: "uniqueDiscriminatorMode"`); the rest are shared across siblings. Today
// the minimizer holes the off-anchor receiver (`ctx.engine` -> `ANYTHING`) but
// keeps the whole nested object verbatim instead of holing it. (Real-spec
// analogue: `moveProcessedInboxAudioNodeToTarget` pins its entire move-options
// object.) The read-off prunes off-anchor statements / call-chains / callbacks
// already; the gap is holing INTO nested object/array literals within a kept
// expression.
minimizer_expectation_case!(
    minimizes_interior_object_arg_holing,
    fixture = "interior_object_arg_holing",
    name = "nested object in a kept call arg keeps only the discriminating property, OBJECT_PROPS for the rest",
    module = "app/nodes",
    bindings = [("SelectedMover", "selectedMover")],
    expected = "expected_match.js",
);

// Single vs group split after the var read-off migration: the multi-target group
// (`SelectedPrimary`/`SelectedSecondary`) still uses the keep-shallow anchor policy
// — both slots keep their direct shallow literals, including the shared
// `enabled: true` over-pin (per-slot tuple resolution remains the cover's job). The
// single-target `SelectedStandalone` now reads its minimal anchor off the shape
// index: `kind: "panel"` alone discriminates it from `sameRouteDifferentKind`, so
// the shared non-discriminating call arg `"settings"` holes to `ANYTHING` and the
// redundant `title: "Settings"` collapses into `OBJECT_PROPS` — sparser and more
// rebuild-robust than the keep-shallow over-pin.
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

// A run of adjacent, near-identical accessor functions collapses into ONE
// binding_group: the source_match is the consecutive run of the four
// `function …Accessor() { return ANYTHING.<key>; }` declarations, with `exports`
// mapping each, rather than four standalone source_match selectors. Each
// accessor is first minimized individually (the shared `resolveContext().services`
// receiver holes to `ANYTHING`, keeping the single discriminating member), then
// the anti-unification grouping pass (readoff_minimization.md item 7) detects
// that the adjacent minimized selectors share the same canonical shape and merges
// the run. Real-spec analogue: `app/state/accessors.yaml`
// `use{AppUser,NodeSpace,FocusService,CoreServices}`, four adjacent
// context-accessor hooks that previously emitted four individual selectors.
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

// General co-occurrence grouping for non-function runs (readoff_minimization.md
// item 5): four adjacent sibling *class* declarations, each individually
// minimized to `class …Card { kind = "<unique>"; CLASS_REST }`, share the same
// canonical selector shape and collapse into ONE binding_group whose
// source_match is the consecutive run, instead of four standalone source_match
// selectors. Exercises that the anti-unification grouping pass is no longer
// function-specific: the same overlap-detection that merges DRY accessor hooks
// now merges a sibling class-declaration cluster.
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
