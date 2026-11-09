//! Report-only effect translation validation: class declaration owners.

use debundle_e2e_support::*;

#[test]
fn inert_class_declaration_closes_only_its_exact_evaluation_owner() {
    let source = "const marker = 1;\n\
class WorkQueue {\n\
  running = 0;\n\
  pending = [];\n\
  constructor(limit) { this.limit = limit; }\n\
  add(job) {\n\
    return new Promise(resolve => { this.pending.push(job); resolve(); });\n\
  }\n\
}\n\
console.log(marker);\n";
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_output(&fixture, "1\n");

    let entry = std::fs::read_to_string(&fixture.entry_path).expect("generated entry source");
    let import = entry.find("import ").expect("class module import");
    let marker = entry
        .find("const marker = 1")
        .expect("residual marker initializer");
    assert!(
        import < marker,
        "class import must run before the residual marker:\n{entry}"
    );

    let report = fixture.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], true, "{report:#}");
    assert_eq!(report["legacy_tv_accepts"], true, "{report:#}");
    assert_eq!(
        report["swapped_pairs"], 1,
        "the class import precedes source unit 0: {report:#}"
    );
    assert_eq!(report["coverage"]["statement_count"], 3, "{report:#}");
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_owner_kind"], 1,
        "only the console call remains outside the initializer subset: {report:#}"
    );
    assert_eq!(report["counts"]["havoc_pairs"], 0, "{report:#}");
}

#[test]
fn unsupported_class_shapes_remain_uncovered() {
    for declaration in [
        "class WorkQueue extends Object {}",
        "class WorkQueue { [globalThis.queueKey]() {} }",
        "class WorkQueue { get value() { return 1; } }",
        "class WorkQueue { static state = (globalThis.marker = 1); }",
        "class WorkQueue { static { globalThis.marker = 1; } }",
    ] {
        let source = format!("{declaration}\n");
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
        ));
        assert_preserved_entry_output(&fixture, "");
        let report = fixture.effect_translation_validation();
        assert_eq!(
            report["coverage"]["owner_coverage_refusals"]["unsupported_owner_kind"], 1,
            "{declaration}: {report:#}"
        );
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], 1,
                "{declaration}: unsupported class shape remains uncovered: {report:#}"
            );
        }
    }
}

#[test]
fn class_instance_initializers_stay_lazy_until_constructor_invocation() {
    let source = "class WorkQueue {\n\
  running = (globalThis.fieldRuns = (globalThis.fieldRuns || 0) + 1);\n\
  pending = [];\n\
  constructor(limit) { this.limit = limit; globalThis.constructed = limit; }\n\
  add(job) { this.pending.push(job); return this.pending.length; }\n\
}\n\
const queue = new WorkQueue(3);\n\
console.log(queue.running, queue.pending.length, queue.limit, globalThis.fieldRuns, globalThis.constructed);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
    ));
    assert_preserved_entry_output(&fixture, "1 0 3 1 3\n");

    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["unknown_coverage"]["global_properties"], 2,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["unknown_coverage"]["general_heap"], 2,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["unknown_coverage"]["host_effects"], 2,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the constructor invocation owner remains uncovered: {report:#}"
    );
}

#[test]
fn class_binding_writes_still_conflict_with_a_tdz_read_when_extraction_reorders_it() {
    let source = "const earlier = WorkQueue;\nclass WorkQueue {}\n";
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[Member::new("earlier"), Member::new("WorkQueue")],
        )],
    ));
    assert_preserved_entry_error(&colocated, "ReferenceError");

    let reordered = run_rejection_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = reordered.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(
        report["coverage"]["unknown_coverage"]["global_properties"], 1,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["unknown_coverage"]["general_heap"], 1,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["unknown_coverage"]["host_effects"], 1,
        "{report:#}"
    );
    let rows = read_jsonl(&reordered.report_path("effect_conflicts.jsonl"));
    let pair = rows
        .iter()
        .find(|pair| {
            pair["cells"].as_array().unwrap().iter().any(|cell| {
                cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "WorkQueue"
            })
        })
        .unwrap_or_else(|| {
            panic!("the class binding write must conflict with the moved TDZ read: {rows:#?}")
        });
    assert_eq!(
        pair["runs_first"]["unit"], 1,
        "the class module is emitted first: {pair:#}"
    );
    assert_eq!(
        pair["runs_later"]["unit"], 0,
        "the residual TDZ read is emitted later: {pair:#}"
    );
    assert_eq!(
        pair["first_opacity"]["unknown_coverage"],
        serde_json::json!([]),
        "the exact class owner is covered even though the moved read is not: {pair:#}"
    );
    let class_cell = pair["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "WorkQueue")
        .unwrap();
    assert_eq!(
        class_cell["first_write"]["kind"], "observed",
        "{class_cell:#}"
    );
    assert_eq!(
        class_cell["later_read"]["kind"], "observed",
        "{class_cell:#}"
    );
    assert_eq!(class_cell["observed_conflict"], true, "{class_cell:#}");
}
