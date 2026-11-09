//! Report-only effect translation validation: owner coverage for audited
//! native constructors and RegExp literals.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

#[test]
fn weak_map_empty_owner_keeps_its_exact_effect_coverage_dependency() {
    const SOURCE: &str = "function publish(key, value) { globalThis[key] = value; }\nconst emptyWeakMap = new WeakMap();\nconst emptyMap = new Map();\nconsole.log(emptyWeakMap ? 'ready' : 'missing', emptyMap ? 'ready' : 'missing');\n";
    let inputs = vec![
        native_constructor_input(
            SOURCE,
            "const emptyWeakMap = new WeakMap();",
            "emptyWeakMap",
            NativeOperationKind::WeakMapEmpty,
        ),
        native_constructor_input(
            SOURCE,
            "const emptyMap = new Map();",
            "emptyMap",
            NativeOperationKind::MapEmpty,
        ),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "maps",
                &[Member::new("emptyWeakMap"), Member::new("emptyMap")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", inputs),
    );
    assert_preserved_entry_output(&fixture, "ready ready\n");

    let report = fixture.effect_translation_validation();
    assert!(report["legacy_gate_accepts"].as_bool().unwrap_or(false));
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["initializer_result_unproved"]
            .as_u64()
            .unwrap_or_default(),
        0,
        "the exact WeakMap and Map sites complete only their named owners: {report:#}"
    );
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"]["native_operation_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "both owner effect receipts retain their exact input dependency: {report:#}"
    );
}

fn empty_native_collection_inputs(
    source: &str,
    declaration_kind: &str,
) -> Vec<NativeOperationInputContract> {
    let map_declaration = format!("{declaration_kind} map = new Map();");
    let set_declaration = format!("{declaration_kind} set = new Set();");
    vec![
        native_constructor_input(
            source,
            &map_declaration,
            "map",
            NativeOperationKind::MapEmpty,
        ),
        native_constructor_input(
            source,
            &set_declaration,
            "set",
            NativeOperationKind::SetEmpty,
        ),
    ]
}

#[test]
fn regexp_literal_creation_covers_only_its_exact_owner() {
    let source = "const marker = 1;\n\
const RegExp = () => { throw new Error('literal invoked RegExp'); };\n\
const pattern = /^x$/;\n\
const matched = pattern.test('x');\n\
console.log(matched, marker);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "regex",
                &[Member::new("RegExp"), Member::new("pattern")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "true 1\n");

    let entry = std::fs::read_to_string(&fixture.entry_path).expect("generated entry source");
    assert!(
        entry.find("import ").unwrap() < entry.find("const marker = 1").unwrap(),
        "the regex module import must run before the residual marker:\n{entry}"
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 2, "{report:#}");
    assert_eq!(report["counts"]["havoc_pairs"], 0, "{report:#}");
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the later pattern.test call remains outside creation coverage: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "the later call and console owner remain uncovered: {report:#}"
        );
    }
}

#[test]
fn regexp_literal_binding_read_write_conflict_remains_visible() {
    let source = "const observed = pattern;\n\
var pattern = /^x$/;\n\
console.log(observed === observed, pattern.test('x'));\n";
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module("regex", &[Member::new("pattern")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    assert!(
        fixture.stderr.contains("atomic-group conflict"),
        "{}",
        fixture.stderr
    );

    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["counts"]["havoc_pairs"], 1, "{report:#}");
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    let creation_pair = rows
        .iter()
        .find(|pair| pair["runs_first"]["unit"] == 1 && pair["runs_later"]["unit"] == 0)
        .unwrap_or_else(|| panic!("the binding conflict must remain visible: {rows:#?}"));
    assert_eq!(creation_pair["havoc"], true, "{creation_pair:#}");
    assert_eq!(
        creation_pair["first_opacity"]["unknown_coverage"],
        serde_json::json!([]),
        "the RegExp creation endpoint itself remains covered: {creation_pair:#}"
    );
    assert_ne!(
        creation_pair["later_opacity"]["unknown_coverage"],
        serde_json::json!([]),
        "the read-side refusal keeps its own Havoc: {creation_pair:#}"
    );
    let binding_cell = creation_pair["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "pattern")
        .unwrap_or_else(|| panic!("the creation binding write remains visible: {creation_pair:#}"));
    assert_eq!(
        binding_cell["first_write"]["kind"], "observed",
        "{binding_cell:#}"
    );
    assert_eq!(
        binding_cell["later_read"]["kind"], "observed",
        "{binding_cell:#}"
    );
    assert_eq!(binding_cell["observed_conflict"], true, "{binding_cell:#}");
}

#[test]
fn regexp_literal_creation_does_not_cover_later_prototype_getters() {
    let source = "const marker = 1;\n\
const pattern = /^x$/;\n\
Object.defineProperty(RegExp.prototype, 'source', {\n\
  configurable: true,\n\
  get() { globalThis.regexGetterReads = (globalThis.regexGetterReads || 0) + 1; return 'overridden'; },\n\
});\n\
Object.defineProperty(pattern, 'custom', {\n\
  configurable: true,\n\
  get() { globalThis.regexOwnGetterReads = (globalThis.regexOwnGetterReads || 0) + 1; return 'own'; },\n\
});\n\
const sourceText = pattern.source;\n\
const missingValue = pattern.notPresent;\n\
const lastIndexValue = pattern.lastIndex;\n\
const customValue = pattern.custom;\n\
console.log(sourceText, missingValue, lastIndexValue, customValue, marker, globalThis.regexGetterReads, globalThis.regexOwnGetterReads);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("regex", &[Member::new("pattern")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "overridden undefined 0 own 1 1 1\n");

    let graph = fixture.dependency_graph();
    let execution = |binding: &str| {
        let node = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .unwrap_or_else(|| panic!("missing {binding} source unit"));
        serde_json::to_value(
            node.synchronous_execution
                .as_ref()
                .unwrap_or_else(|| panic!("missing {binding} execution summary")),
        )
        .expect("serialize execution summary")
    };
    let pattern_execution = execution("pattern");
    assert!(
        pattern_execution["complete"] == true
            && pattern_execution["precision_losses"] == serde_json::json!([]),
        "RegExp literal creation alone remains a complete allocation: {pattern_execution:#?}"
    );
    for binding in [
        "sourceText",
        "missingValue",
        "lastIndexValue",
        "customValue",
    ] {
        let read_execution = execution(binding);
        assert!(
            read_execution["complete"] == false
                && read_execution["precision_losses"]
                    .as_array()
                    .is_some_and(|losses| {
                        losses.iter().any(|loss| {
                            loss["reason"]["code"] == "unsupported_operation"
                                && loss["reason"]["operation"] == "member_read"
                        })
                    }),
            "the {binding} RegExp property read stays outside literal-allocation coverage: {read_execution:#?}"
        );
    }

    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["counts"]["havoc_pairs"], 0, "{report:#}");
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 4,
        "each RegExp property read remains outside creation coverage: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert!(
            report["coverage"]["unknown_coverage"][domain]
                .as_u64()
                .unwrap_or_default()
                >= 1,
            "later prototype mutation, getter read, or console effects stay opaque: {report:#}"
        );
    }
}

#[test]
fn regexp_constructor_initialization_remains_opaque() {
    let source = "const marker = 1;\n\
const RegExp = function () { throw new Error('constructor invoked'); };\n\
const pattern = new RegExp('^x$');\n\
console.log(marker);\n";
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "regex",
                &[Member::new("RegExp"), Member::new("pattern")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_error(&fixture, "constructor invoked");

    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "new RegExp remains outside the literal-only proof: {report:#}"
    );
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    let constructor_pair = rows
        .iter()
        .find(|pair| pair["runs_first"]["unit"] == 2 && pair["runs_later"]["unit"] == 0)
        .unwrap_or_else(|| panic!("the constructor owner must cross the marker: {rows:#?}"));
    assert_eq!(constructor_pair["havoc"], true, "{constructor_pair:#}");
    assert_ne!(
        constructor_pair["first_opacity"]["unknown_coverage"],
        serde_json::json!([]),
        "the constructor endpoint retains unknown coverage: {constructor_pair:#}"
    );
}

#[test]
fn empty_native_collections_cover_only_the_reordered_initializer_owner() {
    const SOURCE: &str = "const earlierMap = (void globalThis.Map, 1);\n\
var map = new Map();\n\
const earlierSet = (void globalThis.Set, 2);\n\
var set = new Set();\n\
const later = map.size + set.size;\n\
console.log(earlierMap, earlierSet, later);\n";
    let inputs = empty_native_collection_inputs(SOURCE, "var");
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "collections",
                &[Member::new("map"), Member::new("set")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", inputs),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_output(&fixture, "1 2 0\n");

    let entry = std::fs::read_to_string(&fixture.entry_path).expect("generated entry source");
    let import = entry.find("import ").expect("collection module import");
    let earlier = entry
        .find("const earlierMap")
        .expect("residual harmless read");
    assert!(
        import < earlier,
        "the empty collection constructors must actually move before the residual reads:\n{entry}"
    );
    for property in ["Map", "Set"] {
        assert!(
            entry.contains(&format!("globalThis.{property}")),
            "the residual GlobalProp({property}) read must remain in emitted code:\n{entry}"
        );
    }
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/collections.js",
        &["var map = new Map();", "var set = new Set();"],
        &[],
    );

    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"]["native_operation_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "coverage of the two native owners retains their exact author premises: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 4,
            "the two direct constructors alone are covered; sequence reads, later observations, and console remain opaque: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 3,
        "the sequence and later property reads retain their own refusal: {report:#}"
    );
}

#[test]
fn empty_native_collection_owner_does_not_hide_prior_var_binding_reads() {
    const SOURCE: &str = "const earlierMap = map;\n\
var map = new Map();\n\
const earlierSet = set;\n\
var set = new Set();\n\
console.log(earlierMap === undefined, earlierSet === undefined, map.size + set.size);\n";
    let inputs = empty_native_collection_inputs(SOURCE, "var");
    let colocated = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "all",
                &[
                    Member::new("earlierMap"),
                    Member::new("map"),
                    Member::new("earlierSet"),
                    Member::new("set"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", inputs.clone()),
    );
    assert_preserved_entry_output(&colocated, "true true 0\n");

    let reordered = run_rejection_fixture_with_args(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "collections",
                &[Member::new("map"), Member::new("set")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", inputs),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = reordered.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    let rows = read_jsonl(&reordered.report_path("effect_conflicts.jsonl"));
    for binding in ["map", "set"] {
        let pair = rows
            .iter()
            .find(|pair| {
                pair["cells"].as_array().unwrap().iter().any(|cell| {
                    cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == binding
                })
            })
            .unwrap_or_else(|| panic!("reordered {binding} write/read pair: {rows:#?}"));
        let cell = pair["cells"]
            .as_array()
            .unwrap()
            .iter()
            .find(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == binding)
            .unwrap();
        assert_eq!(cell["first_write"]["kind"], "observed", "{cell:#}");
        assert_eq!(cell["later_read"]["kind"], "observed", "{cell:#}");
        assert_eq!(cell["observed_conflict"], true, "{cell:#}");
        assert_eq!(
            pair["first_opacity"]["unknown_coverage"],
            serde_json::json!([])
        );
    }
}

#[test]
fn text_encoder_initializer_covers_only_its_exact_root_owner() {
    const SOURCE: &str = "const earlier = (void globalThis.TextEncoder, 1);\n\
var encoder = new TextEncoder();\n\
const later = encoder.encoding;\n\
console.log(earlier, later);\n";
    let input = native_constructor_input(
        SOURCE,
        "var encoder = new TextEncoder();",
        "encoder",
        NativeOperationKind::TextEncoderEmpty,
    );
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module("encoder", &[Member::new("encoder")])],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", vec![input]),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_output(&fixture, "1 utf-8\n");

    let entry = std::fs::read_to_string(&fixture.entry_path).expect("generated entry source");
    let import = entry.find("import ").expect("TextEncoder module import");
    let earlier = entry
        .find("const earlier")
        .expect("residual constructor-global read");
    assert!(
        import < earlier,
        "the certified initializer must move before the harmless residual read:\n{entry}"
    );
    assert!(
        entry.contains("globalThis.TextEncoder"),
        "the residual global read remains in the output:\n{entry}"
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/encoder.js",
        &["var encoder = new TextEncoder();"],
        &[],
    );

    let report = fixture.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], true, "{report:#}");
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 3,
            "only the direct TextEncoder initializer is covered; the sequence read, later property read, and console call remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 2,
        "the wrapped global read and later property read retain their own refusals: {report:#}"
    );
}

#[test]
fn text_encoder_initializer_requires_identity_zero_arguments_and_root_shape() {
    for (source, reason, unknown_statements) in [
        (
            "var encoder = new TextEncoder();\n\
             globalThis.TextEncoder = function replacement() {};\n",
            "initializer_result_unproved",
            2,
        ),
        (
            "var encoder = new TextEncoder('utf-8');\n",
            "unsupported_initializer",
            1,
        ),
        (
            "var encoder = (void globalThis.marker, new TextEncoder());\n",
            "unsupported_initializer",
            1,
        ),
    ] {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        assert_preserved_entry_output(&fixture, "");
        let report = fixture.effect_translation_validation();
        assert_eq!(
            report["coverage"]["owner_coverage_refusals"][reason], 1,
            "unproved TextEncoder initializer: {source}: {report:#}"
        );
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], unknown_statements,
                "unproved TextEncoder initializer remains opaque: {source}: {report:#}"
            );
        }
    }

    // A transfer from the prior direct initializer must not escape through a
    // nested call or close a later transient initializer.
    let source = "var encoder = new TextEncoder();\n\
const transient = (() => new TextEncoder())();\n";
    let input = native_constructor_input(
        source,
        "var encoder = new TextEncoder();",
        "encoder",
        NativeOperationKind::TextEncoderEmpty,
    );
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![]).with_native_operation_inputs("static/app", vec![input]),
    );
    assert_preserved_entry_output(&fixture, "");
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the nested/transient call keeps its own refusal: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "only the direct initializer is covered: {report:#}"
        );
    }
}

#[test]
fn text_encoder_owner_coverage_keeps_prior_var_binding_read_observed() {
    const SOURCE: &str = "const earlier = encoder;\n\
var encoder = new TextEncoder();\n\
console.log(earlier === undefined, encoder.encoding);\n";
    let input = native_constructor_input(
        SOURCE,
        "var encoder = new TextEncoder();",
        "encoder",
        NativeOperationKind::TextEncoderEmpty,
    );
    let opts = |members| {
        FixtureOpts::new(SOURCE, vec![logical_module("encoder", members)])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![input.clone()])
    };
    let colocated = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "all",
                &[Member::new("earlier"), Member::new("encoder")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", vec![input.clone()]),
    );
    assert_preserved_entry_output(&colocated, "true utf-8\n");

    let reordered = run_rejection_fixture_with_args(
        opts(&[Member::new("encoder")]),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = reordered.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    let rows = read_jsonl(&reordered.report_path("effect_conflicts.jsonl"));
    let pair = rows
        .iter()
        .find(|pair| {
            pair["cells"].as_array().unwrap().iter().any(|cell| {
                cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "encoder"
            })
        })
        .unwrap_or_else(|| panic!("reordered encoder read/write pair: {rows:#?}"));
    let cell = pair["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "encoder")
        .unwrap();
    assert_eq!(cell["first_write"]["kind"], "observed", "{cell:#}");
    assert_eq!(cell["later_read"]["kind"], "observed", "{cell:#}");
    assert_eq!(cell["observed_conflict"], true, "{cell:#}");
}
