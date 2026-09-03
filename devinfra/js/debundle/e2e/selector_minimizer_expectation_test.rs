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

use debundle_e2e_support::{parse_stdout_json, run_synthesize_selectors, write_text_file};

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

// Multi-target binding-group read-off (plan item 3): a group of sibling key-set
// objects, two of them exported targets, each discriminated only by its own
// uniquely-present key (`logViewer` / `alertChip`, values shared `theme.base`
// member accesses holed to `ANYTHING`). The binding-group read-off reads each
// target declarator slot's minimal anchor off the shape index + a slot-aware
// greedy, restricts the kept spans to that slot, UNIONs them, and proves the
// tuple through the binding-group matcher — so each slot pins only its
// discriminating key with `ANYTHING` for the gaps and a `DECLARATORS` gap for
// the non-target third declarator, instead of the keep-shallow path's over-pin
// (which, with every value already a non-literal member access, would escalate to
// keeping *every* key of both target objects). The per-slot declarator-tuple
// resolution the chunk-wide read-off cannot express.
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

// A target class among many same-shape sibling classes minimizes to `ANYTHING;`
// class-member holes plus the one member run carrying the discriminating value anchor (the
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
// to `ANYTHING` holes on both sides of the single discriminating key, so the
// selector survives key reordering. W3 routed the single-target object form
// through the read-off shape index + padded-run-hole renderer, replacing the
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
// discriminating class field, with `ANYTHING;` holes absorbing every other
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

// A binding initialized by a deeply nested call tree minimizes to ANYTHING-holed
// outer callees and an `ARGS` run-hole for their non-anchor sibling arguments,
// drilling only to the one deep object literal that carries the discriminating
// key. The var read-off drills into the nested call tree (`hole_expr` →
// `hole_callee` / `hole_args`), holes every bare-function callee
// (`wrapOuter`/`decorate`/`buildInner`) to `ANYTHING` — a minified name the matcher
// alpha-wildcards anyway, never a chosen anchor — and collapses the dropped
// `{ theme: … }` sibling argument into a variadic `ARGS` run-hole, so a rebuild
// that adds or drops a sibling argument still resolves.
minimizer_expectation_case!(
    minimizes_deeply_nested_call_args,
    fixture = "deeply_nested_call_args",
    name = "deeply nested call tree keeps only the discriminating leaf literal",
    module = "app/views",
    bindings = [("SelectedView", "selectedView")],
    expected = "expected_match.js",
);

// A target object inside a multi-declarator group of sibling enum/lookup objects
// minimizes to DECLARATORS holes around the target declarator plus `ANYTHING`
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
// with `ANYTHING` holes for the rest, and DECLARATORS holes for the sibling
// declarators, instead of keeping every key of the target object. Mirrors the
// real gaffer CSS-styles dicts (`{ diagnosticsSection: …, detailsToggle: …, … }`)
// kept whole inside `DECLARATORS`-bracketed groups.
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
// keeps exactly those two non-adjacent keys, each surrounded by `ANYTHING` so
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
// map) minimizes to `ANYTHING` holes on both sides of the one entry whose nested
// value carries the discriminating literal, and that entry's own nested object
// itself collapses to the discriminating property plus `ANYTHING`. Generalizes
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
// the *shortest* discriminator and holes everything else with `ANYTHING`. Here
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
// minimize to `ANYTHING` holes around the single destructured property that
// discriminates this target from its siblings, plus STMT_LIST holes for the
// rest of the body. Today the minimizer keeps the entire wide destructuring
// pattern whole — every destructured name — even when one anchored property
// (and one body statement) would resolve uniquely, because the destructure
// block is a single large node it retains intact rather than holing its
// property run. Mirrors the real spec's React components whose 10-25-name
// `{ ... } = e` prop destructure is kept verbatim at the top of an otherwise
// holed body.
minimizer_expectation_case!(
    minimizes_wide_destructure_block,
    fixture = "wide_destructure_block",
    name = "wide destructuring block keeps only the discriminating destructured property",
    module = "app/components",
    bindings = [("SelectedComponent", "selectedComponent")],
    expected = "expected_match.js",
);

// A SINGLE-target class with no same-shape sibling holes its body down to one
// stable value anchor, absorbing the other members with `ANYTHING;`. The empty
// scaffold `class SelectedRunner { ANYTHING; }` resolves uniquely (it is the only
// class) but pins nothing rebuild-stable (criterion 5); the robustness-anchor
// policy instead drills to a value anchor. The *minimal* anchor is
// `NumberLiteral("0")` — but `0` occurs three times in the body (the two `= 0`
// fields and the `void 0` inside the constructor's sequence), so pinning it keeps
// all three sites and drags in unrelated members. The read-off's span preference
// therefore defers that multi-occurrence anchor and walks the single-occurrence
// value anchors best-first, landing on the cheapest one — the `name` object key in
// the constructor's `boxedStatus = box(…, { name: … })` — holing the receiver, the
// `box` callee, the `"stopped"` argument, the value, and every other sequence
// element. The constructor body holes through the `hole_expr` sequence/ternary arms
// (the same arms that fix `class_sequence_constructor_body`); before they landed
// `0` rendered to a verbatim constructor the matcher rejected, masking this choice.
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
// keeps `ANYTHING("div", ARGS)` — anchoring on the unique `"div"` tag literal — with
// the prop destructure and hooks absorbed into `STMT_LIST`. The bare callees
// `wrap`/`jsx` hole to `ANYTHING` (the `hole_callee` policy holes a minified
// bare-function reference the matcher alpha-wildcards anyway), and the dropped
// props object after the `"div"` tag collapses into an `ARGS` run-hole. The real
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
// hole its non-anchor properties to `ANYTHING`, the same way the single-target
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
    name = "nested object in a kept call arg keeps only the discriminating property, run holes for the rest",
    module = "app/nodes",
    bindings = [("SelectedMover", "selectedMover")],
    expected = "expected_match.js",
);

// Single vs group split after the var read-off migration: the multi-target group
// (`SelectedPrimary`/`SelectedSecondary`) now reads off per-slot minimal anchors
// (binding-group read-off, plan item 3). Each slot pins only what singles its own
// declarator out within the statement: slot 0's `enabled: true` (vs
// `unrelatedPrimary`'s `enabled: false`) is enough, so its leading `makeEntry`
// argument drops into an `ARGS` run-hole (and the minified `makeEntry` callee holes
// to `ANYTHING`); slot 1 still needs `"secondary"` because `{ enabled: true }`
// alone would also fit slot 0. The per-slot kept spans union and the binding-group
// matcher proves the tuple — sparser than the keep-shallow path's "keep every
// slot's shallow literals" (which pinned both `"primary"` and `"secondary"`). The
// single-target `SelectedStandalone` reads its minimal anchor off the shape index:
// `kind: "panel"` alone discriminates it from `sameRouteDifferentKind`, so the
// shared non-discriminating leading call arg `"settings"` drops into an `ARGS`
// run-hole (the minified `registerRoute` callee holes to `ANYTHING`) and the
// redundant `title: "Settings"` collapses into an `ANYTHING` run hole.
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
// the anti-unification grouping pass detects
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

// General co-occurrence grouping for non-function runs: four adjacent sibling
// *class* declarations, each individually
// minimized to `class …Card { kind = "<unique>"; ANYTHING; }`, share the same
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

// Enclosing-context anchoring (#2315): an **alpha-only construct** — a target
// `const selectedHelper = new Factory()` among same-shape `const X = new IDENT()`
// siblings — has no value anchor of its own (the `new` callee alpha-canonicalizes,
// the call has no stable args), so the target's read-off and keep-shallow paths
// both fail. Rather than leaving it name-pinned, the var residual path pins a
// **stable adjacent declaration** as context: a 2-statement window pairing the
// immediately-preceding `defineSelected("gamma-unique-token")` call (holed to
// `ANYTHING("gamma-unique-token")` — its minified callee dropped per #2318, only
// the globally-unique string pinned) with the target's degenerate
// `const SelectedHelper = ANYTHING` scaffold, `target_binding` picking the target
// out. Exercises the
// neighbor-*before*-target window (`target_binding` at needle index 1, routed
// through the single-declarator-target matcher). Real-spec analogue: the alpha-only
// `new ()` factories the read-off otherwise reports as residual debt.
minimizer_expectation_case!(
    anchors_alpha_only_construct_to_a_stable_neighbor,
    fixture = "neighbor_context_alpha_construct",
    name = "alpha-only `new` construct is anchored to its stable adjacent call",
    module = "app/helpers",
    bindings = [("SelectedHelper", "selectedHelper")],
    expected = "expected_match.js",
);

// Enclosing-context anchoring (#2315): a **near-duplicate emitted helper** — a
// target `function selectedHelper() { return wrap(); }` among byte-identical
// `function X() { return wrap(); }` siblings (the `__decorate`-family shape) — has
// no discriminating feature inside its own declaration, so even the bare
// `function SelectedHelper() { STMT_LIST }` scaffold matches every sibling. The
// function read-off (`read_off_candidates`) falls to the target's stable
// neighbors: a 2-statement window pairing the holed scaffold with the
// immediately-following `registerSelected("delta-unique-token")` call holed to
// `ANYTHING("delta-unique-token")` (minified callee dropped per #2318, unique
// string pinned), `target_binding` selecting the function. Closes the residual the
// in-body value cover (#2289) explicitly left open.
minimizer_expectation_case!(
    anchors_duplicate_helper_to_a_stable_neighbor,
    fixture = "neighbor_context_duplicate_helper",
    name = "near-duplicate helper function is anchored to its stable adjacent call",
    module = "app/helpers",
    bindings = [("SelectedHelper", "selectedHelper")],
    expected = "expected_match.js",
);

// IGNORED (dogfood over-pin, 2026-06-17): enclosing-context anchoring (#2315)
// picks the right stable neighbor but, when that neighbor is a **function
// declaration** rather than a single call statement, pins the neighbor's body
// VERBATIM instead of holing it down to its own discriminating anchor. Here the
// near-duplicate target `selectedHelper` (bare scaffold matches `firstHelper`)
// is correctly anchored to the following `neighborWithToken` declaration, but the
// emitted selector keeps all three of the neighbor's body statements
// (`const prepared = …`, the discriminating `emit("neighbor-unique-token")`, and
// `cleanup(prepared)`) plus its parameter list. The target shape is the neighbor
// minimized as if it were a standalone function read-off: hole the name/params,
// `STMT_LIST` the non-discriminating statements, and keep only the unique
// `ANYTHING("neighbor-unique-token")` anchor (callee dropped per #2318). The
// neighbor branch of `render_via_neighbor_context` needs to run the neighbor
// declaration back through the per-form read-off before pinning it. Dominant
// dogfood over-pin shape: 46/62 of the gaffer `78d928dca7` >40-line, ≤2-hole
// conversions are this "neighbor declaration kept whole" pattern (e.g. the real
// `SubscriptionFlow`/`nodeDisplayName` wrappers whose preceding ~40-line component
// function is pinned verbatim).
minimizer_expectation_case!(
    #[ignore = "neighbor-context anchoring pins the whole neighbor function declaration instead of holing it to its anchor"]
    minimizes_neighbor_context_whole_function_neighbor,
    fixture = "neighbor_context_whole_function_neighbor",
    name = "neighbor function declaration is holed to its discriminating anchor, not pinned whole",
    module = "app/helpers",
    bindings = [("SelectedHelper", "selectedHelper")],
    expected = "expected_match.js",
);

// IGNORED (dogfood over-pin, 2026-06-17): a class-EXPRESSION-valued `const`
// (`const X = class { … }`) is pinned whole because the var read-off
// (`try_var_read_off` → `hole_expr`) has no `Expr::Class` arm, so the class
// initializer never routes through the class read-off (`ANYTHING;` member holing).
// The equivalent class DECLARATION minimizes correctly today — control:
// `class selectedStore { status = "idle"; run(){…} describe(){…} }` emits
// `class SelectedStore { status = "idle"; ANYTHING; }` — so the gap is purely the
// expression form failing to reach `minimize_class_selector_candidates`'s read-off. The
// target shape wraps that same class read-off output back in the `const … = class`
// initializer. Real-spec analogue: the 0-hole whole-body giants
// `integrations/google/api/client.yaml::GoogleApiClient` (314 lines) and
// `features/search/state.yaml::SearchState` (300 lines), both `const X = class {…}`
// kept verbatim with zero holes.
minimizer_expectation_case!(
    #[ignore = "class-expression-valued const is pinned whole; not routed through the class read-off"]
    minimizes_class_expression_const_whole_body,
    fixture = "class_expression_const_whole_body",
    name = "class-expression const keeps only one discriminating member, not the whole class body",
    module = "app/stores",
    bindings = [("SelectedStore", "selectedStore")],
    expected = "expected_match.js",
);

// A class whose only discriminating content sits inside its constructor's
// **sequence (comma) expression** body
// (`constructor(...) { (super(a), this.x = b, this.label = "token"); }`) pins by
// that own anchor, not via a neighbor. `hole_expr`'s `Expr::Seq` arm holes each
// non-anchor element to `ANYTHING` and recurses into the anchored one, and the
// matcher sees through the source's `(a, b, c)` parens, so the read-off keeps the
// discriminating `this.label = "selected-error-token"` (holed to
// `ANYTHING.label = "selected-error-token"`). Without those two pieces the
// sequence stayed verbatim (raw sibling subtrees the prove-gate rejects), the bare
// scaffold was ambiguous among same-shape sibling error classes, and the class
// fell to `render_via_neighbor_context`, pinning the unrelated preceding
// `serializeState` neighbor whole — the over-pin this case originally captured.
minimizer_expectation_case!(
    minimizes_class_sequence_constructor_body,
    fixture = "class_sequence_constructor_body",
    name = "class anchor inside a constructor sequence expression is holed in place, not pinned via a neighbor",
    module = "app/errors",
    bindings = [("SelectedError", "selectedError")],
    expected = "expected_match.js",
);
