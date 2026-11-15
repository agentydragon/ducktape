//! Report-only effect translation validation: class declaration owners.

use debundle_e2e_support::*;

#[test]
fn deferred_class_members_do_not_execute_when_the_class_moves() {
    for body in [
        "get value() { globalThis.classRan = true; return 1; } set value(value) { globalThis.classRan = value; }",
        "#value = (() => { throw 'deferred field'; })(); get value() { return this.#value; }",
        "constructor(value = (() => { throw 'deferred default'; })()) { globalThis.classRan = value; } read({ value } = {}) { return value; }",
        "async run(value = (() => { throw 'deferred async default'; })()) { globalThis.classRan = value; } *values() { globalThis.classRan = true; yield 1; }",
        "#value; get #read() { globalThis.classRan = true; return this.#value; } set #read(value) { this.#value = value; } read() { return this.#read; }",
    ] {
        let source = format!(
            "const marker = 1;\nclass WorkQueue {{ {body} }}\nconsole.log(marker, globalThis.classRan);\nexport {{ marker, WorkQueue }};\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
            )
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, "1 undefined\n");
        assert!(
            fixture.effect_translation_validation()["swapped_pairs"]
                .as_u64()
                .is_some_and(|count| count > 0),
            "the class must actually move across marker: {body}"
        );
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                fixture.effect_translation_validation()["coverage"]["unknown_coverage"][domain],
                1,
                "only output remains uncovered; class creation has its own receipt: {body}"
            );
        }
    }
}

#[test]
fn eager_class_expressions_cannot_inherit_deferred_creation_coverage() {
    for declaration in [
        "class WorkQueue extends stop() {}",
        "class WorkQueue { [stop()]() {} }",
        "class WorkQueue { static value = stop(); }",
        "class WorkQueue { static #value = stop(); }",
        "class WorkQueue { static { stop(); } }",
    ] {
        let source = format!(
            "function stop() {{ throw 'eager class'; }}\nconst marker = 1;\n{declaration}\nconsole.log(marker);\nexport {{ marker, WorkQueue }};\n"
        );
        let refused = run_rejection_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
            )
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_node_error(&refused.source_root.join("static/app.js"), "eager class");
        assert!(!refused.entry_path().exists(), "{declaration}");
    }
}

#[test]
fn moving_a_class_keeps_a_host_setters_captured_cell_in_tdz() {
    let opts = || {
        FixtureOpts::new(
        "globalThis.saved = () => WorkQueue;\nclass WorkQueue { #pending = []; get size() { return this.#pending.length; } }\nconsole.log(typeof WorkQueue);\n",
        vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
    ).with_unassigned_mode(unassigned_mode_inline())
    };
    let refused = run_rejection_fixture(opts());
    assert!(!refused.entry_path().exists());

    let diagnostic = run_emit_on_rejection_fixture(opts());
    assert!(diagnostic.gate_rejected_marker().is_file());
    let setup = "Object.defineProperty(globalThis, 'saved', { set(read) { try { read(); console.log('ready'); } catch (error) { console.log(error.name); } }, configurable: true });\n";
    let source_driver = diagnostic.source_root.join("class-setter.mjs");
    write_text_file(
        &source_driver,
        &format!("{setup}await import('./static/app.js');\n"),
    );
    assert_node_output(&source_driver, "ReferenceError\nfunction\n", "");
    let emitted_driver = diagnostic.app_root().join("class-setter.mjs");
    write_text_file(
        &emitted_driver,
        &format!("{setup}await import('./static/app/entry.js');\n"),
    );
    assert_node_output(&emitted_driver, "ready\nfunction\n", "");
}

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
        "class WorkQueue { static #state = 1; }",
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
