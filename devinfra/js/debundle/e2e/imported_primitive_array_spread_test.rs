//! Exact, source-bound inputs let one imported primitive-array spread retain
//! native yields while keeping provider ownership and caller effects visible.

use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use spec::{
    ImportedPrimitiveArrayLifetimeContract, ImportedPrimitiveArrayOperandContract,
    ImportedPrimitiveArraySpreadInputContract,
};

const EMPTY_PROVIDER: &str =
    "var first = [];\nconst second = [];\nexport { first as left, second as right };\n";
const NONEMPTY_PROVIDER: &str =
    "var first = [\"a\"];\nconst second = [\"b\"];\nexport { first as left, second as right };\n";
const CONSUMER_IMPORTS: &str =
    "import { left as inputLeft, right as inputRight } from './provider.js';\n";
const CONSUMER_SPREAD: &str = "const merged = [...inputLeft, ...inputRight];\n";
const CONSUMER_SELECTOR: &str = "const result = [...left, ...right];";

fn input(
    consumer: &str,
    provider: &str,
    consumer_digest: Option<&str>,
    provider_digest: Option<&str>,
    first_provider_pattern: &str,
    second_provider_pattern: &str,
) -> ImportedPrimitiveArraySpreadInputContract {
    ImportedPrimitiveArraySpreadInputContract {
        source_match: source_match_selector(CONSUMER_SELECTOR, &["result"]),
        operands: vec![
            ImportedPrimitiveArrayOperandContract {
                selector_local: "left".to_string(),
                provider_chunk: "static/provider".to_string(),
                provider_source_match: source_match_selector(
                    first_provider_pattern.trim(),
                    &["values"],
                ),
                audited_provider_source_sha256: source_sha256(provider_digest.unwrap_or(provider)),
            },
            ImportedPrimitiveArrayOperandContract {
                selector_local: "right".to_string(),
                provider_chunk: "static/provider".to_string(),
                provider_source_match: source_match_selector(
                    second_provider_pattern.trim(),
                    &["values"],
                ),
                audited_provider_source_sha256: source_sha256(provider_digest.unwrap_or(provider)),
            },
        ],
        audited_source_sha256: source_sha256(consumer_digest.unwrap_or(consumer)),
        lifetime: ImportedPrimitiveArrayLifetimeContract {
            identity_unchanged_through_spread: true,
            dense_own_data_primitive_contents_unchanged_through_spread: true,
            ordinary_array_prototype_chain_unchanged_through_spread: true,
            no_own_iterator_or_accessors_through_spread: true,
            no_rebinding_through_spread: true,
        },
        note: Some("Neutral fixture audits this exact imported spread lifetime.".to_string()),
    }
}

fn empty_consumer_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("arrays/merged", &[Member::new("merged")]),
        logical_module("callbacks/skipped", &[Member::new("skipped")]),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

fn nonempty_consumer_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("arrays/merged", &[Member::new("merged")]),
        logical_module(
            "callbacks/effects",
            &[Member::new("found"), Member::new("callbackCalls")],
        ),
    ]
}

fn provider_modules() -> Vec<LogicalModuleEntry> {
    vec![logical_module(
        "arrays/provider",
        &[
            Member::renamed("left", "first"),
            Member::renamed("right", "second"),
        ],
    )]
}

fn provider_modules_with_guard(guard: &str) -> Vec<LogicalModuleEntry> {
    vec![logical_module_with_anon(
        "arrays/provider",
        &[
            Member::renamed("left", "first"),
            Member::renamed("right", "second"),
        ],
        &[guard],
    )]
}

fn run_configured(
    consumer: &str,
    provider: &str,
    consumer_logical_modules: Vec<LogicalModuleEntry>,
    contract: ImportedPrimitiveArraySpreadInputContract,
) -> Fixture {
    let chunks = [("static/provider", provider)];
    let provider_logical_modules = [("static/provider", provider_modules())];
    run_fixture_with_graphs(
        FixtureOpts::new(consumer, consumer_logical_modules)
            .with_extra_chunks(&chunks)
            .with_extra_chunk_logical_modules(&provider_logical_modules)
            .with_imported_primitive_array_spread_inputs("static/app", vec![contract]),
    )
}

fn admitted_builtin_modules(modules: &[NodeBuiltinModule]) -> HostBehavior {
    HostBehavior {
        reentry_free_builtin_modules: modules.iter().copied().collect(),
        ..HostBehavior::default()
    }
}

fn run_rejected_builtin_closure_fixture(
    consumer: &str,
    provider: &str,
    helper: &str,
    contract: ImportedPrimitiveArraySpreadInputContract,
    host_behavior: HostBehavior,
) -> RejectedFixture {
    let chunks = [("static/provider", provider), ("static/helper", helper)];
    let provider_logical_modules = [
        ("static/provider", provider_modules()),
        (
            "static/helper",
            vec![logical_module("values/helper", &[Member::new("marker")])],
        ),
    ];
    run_rejection_fixture(
        FixtureOpts::new(consumer, empty_consumer_modules())
            .with_extra_chunks(&chunks)
            .with_extra_chunk_logical_modules(&provider_logical_modules)
            .with_imported_primitive_array_spread_inputs("static/app", vec![contract])
            .with_host_behavior(host_behavior),
    )
}

fn run_rejected(
    consumer: &str,
    provider: &str,
    contract: Option<ImportedPrimitiveArraySpreadInputContract>,
) -> RejectedFixture {
    let chunks = [("static/provider", provider)];
    let provider_logical_modules = [("static/provider", provider_modules())];
    let opts = FixtureOpts::new(consumer, empty_consumer_modules())
        .with_extra_chunks(&chunks)
        .with_extra_chunk_logical_modules(&provider_logical_modules);
    run_rejection_fixture_with_graphs(match contract {
        Some(contract) => {
            opts.with_imported_primitive_array_spread_inputs("static/app", vec![contract])
        }
        None => opts,
    })
}

fn run_rejected_without_provider_ownership(
    consumer: &str,
    provider: &str,
    contract: ImportedPrimitiveArraySpreadInputContract,
) -> RejectedFixture {
    let chunks = [("static/provider", provider)];
    let provider_logical_modules = [("static/provider", Vec::new())];
    run_rejection_fixture(
        FixtureOpts::new(consumer, empty_consumer_modules())
            .with_extra_chunks(&chunks)
            .with_extra_chunk_logical_modules(&provider_logical_modules)
            .with_imported_primitive_array_spread_inputs("static/app", vec![contract]),
    )
}

fn assert_empty_spread_schedule_refusal(rejected: &RejectedFixture) {
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "{}",
        rejected.stderr
    );
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        "0 false 0 1 0 true\n",
        "",
    );
    assert_node_output(&rejected.entry_path(), "0 false 0 1 0 true\n", "");
    assert!(
        rejected.entry_path().is_file(),
        "rejected candidate entry is emitted"
    );
    for path in [
        "static/app/modules/arrays/merged.js",
        "static/app/modules/callbacks/skipped.js",
        "static/app/modules/values/later.js",
    ] {
        assert!(
            !rejected.out_root.join(path).exists(),
            "a rejected schedule must not publish a partial module layout: {path}"
        );
    }
}

fn empty_consumer() -> String {
    format!(
        "{CONSUMER_IMPORTS}{CONSUMER_SPREAD}\
         const skipped = merged.some(() => later > 0);\n\
         const later = 0;\n\
         let unrelatedCalls = 0;\n\
         const unrelated = [1].some(() => {{ unrelatedCalls++; return true; }});\n\
         console.log(merged.length, skipped, later, unrelatedCalls, inputLeft.length, unrelated);\n\
         export {{ merged, skipped, later, unrelatedCalls, unrelated }};\n"
    )
}

fn nonempty_consumer() -> String {
    format!(
        "{CONSUMER_IMPORTS}{CONSUMER_SPREAD}\
         let callbackCalls = 0;\n\
         const found = merged.some((value) => {{ callbackCalls++; return value === 'b'; }});\n\
         inputLeft.push('later');\n\
         console.log(merged[0], merged[1], found, callbackCalls, inputLeft.length);\n\
         export {{ merged, found, callbackCalls }};\n"
    )
}

#[test]
fn empty_imported_spread_keeps_runtime_and_reports_schedule_refusal() {
    let consumer = empty_consumer();
    let chunks = [("static/provider", EMPTY_PROVIDER)];
    let provider_logical_modules = [("static/provider", provider_modules())];
    let uncontracted = run_rejection_fixture_with_graphs_and_args(
        FixtureOpts::new(&consumer, empty_consumer_modules())
            .with_extra_chunks(&chunks)
            .with_extra_chunk_logical_modules(&provider_logical_modules),
        &["--emit-on-rejection"],
        3,
    );
    assert_empty_spread_schedule_refusal(&uncontracted);
    let graph = uncontracted.dependency_graph();
    let merged = source_unit_for_binding(&graph, "merged");
    let merged_node = graph.nodes.iter().find(|node| node.id == merged).unwrap();
    let execution = merged_node
        .synchronous_execution
        .as_ref()
        .expect("source spread execution evidence");
    assert!(execution.complete, "{execution:#?}");
    let execution_value = serde_json::to_value(execution).expect("serialize spread execution");
    assert_eq!(
        execution_value["binding_effect_authority"]["authority"], "operation_receipt",
        "the actual spread initializer keeps its operation receipt: {execution_value:#}"
    );
    assert_eq!(
        execution_value["binding_effect_authority"]["receipt"]["source_module"], "static/app",
        "the receipt belongs to the actual spread initializer: {execution_value:#}"
    );
    let observations = execution_value["native_prefix_observations"]
        .as_array()
        .expect("native prefix observations");
    assert!(
        observations.len() == 2
            && observations
                .iter()
                .all(|observation| observation["guard"] == "array_intrinsics"),
        "the empty spread is certified by the two array-integrity observations, without an authored lifetime contract: {execution_value:#}"
    );

    let skipped = source_unit_for_binding(&graph, "skipped");
    let later = source_unit_for_binding(&graph, "later");
    assert!(
        !graph.edges.iter().any(|edge| {
            edge.source == skipped && edge.target == later && edge.constrains_init_order
        }),
        "the empty array keeps its callback deferred: {:#?}",
        graph.edges
    );

    let contract = input(
        &consumer,
        EMPTY_PROVIDER,
        None,
        None,
        "var values = [];",
        "const values = [];",
    );
    let contract_opts = FixtureOpts::new(&consumer, empty_consumer_modules())
        .with_extra_chunks(&chunks)
        .with_extra_chunk_logical_modules(&provider_logical_modules)
        .with_imported_primitive_array_spread_inputs("static/app", vec![contract]);
    let rejected =
        run_rejection_fixture_with_graphs_and_args(contract_opts, &["--emit-on-rejection"], 3);
    assert_empty_spread_schedule_refusal(&rejected);
    let contracted_graph = rejected.dependency_graph();
    let merged = source_unit_for_binding(&contracted_graph, "merged");
    let merged_node = contracted_graph
        .nodes
        .iter()
        .find(|node| node.id == merged)
        .unwrap();
    let execution = merged_node
        .synchronous_execution
        .as_ref()
        .expect("contracted spread execution evidence");
    assert_eq!(
        execution.imported_primitive_array_spread_input_uses.len(),
        2
    );
    let evidence = &execution.imported_primitive_array_spread_input_uses[0].evidence;
    assert_eq!(evidence.operands[0].selector_local, "left");
    assert_eq!(evidence.operands[0].provider_module, "static/provider");
    assert_eq!(evidence.operands[0].provider_export, "left");
    assert_eq!(evidence.operands[0].provider_local, "first");
    assert!(
        evidence
            .lifetime
            .ordinary_array_prototype_chain_unchanged_through_spread
    );
}

#[test]
fn nonempty_imported_spread_keeps_callback_effects_and_later_provider_mutation() {
    let consumer = nonempty_consumer();
    let fixture = run_configured(
        &consumer,
        NONEMPTY_PROVIDER,
        nonempty_consumer_modules(),
        input(
            &consumer,
            NONEMPTY_PROVIDER,
            None,
            None,
            "var values = ['a'];",
            "const values = ['b'];",
        ),
    );
    assert_preserved_entry_output(&fixture, "a b true 2 2\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/arrays/merged.js",
        &["const merged", "inputLeft", "inputRight"],
        &[],
    );
    assert_source_file(&fixture.entry_path, &["inputLeft.push"], &[]);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/callbacks/effects.js",
        &["callbackCalls++"],
        &[],
    );
    let graph = fixture.dependency_graph();
    let merged = source_unit_for_binding(&graph, "merged");
    let merged_node = graph.nodes.iter().find(|node| node.id == merged).unwrap();
    let uses = &merged_node
        .synchronous_execution
        .as_ref()
        .expect("spread execution evidence")
        .imported_primitive_array_spread_input_uses;
    assert_eq!(uses.len(), 2);
    assert_eq!(uses[0].evidence.operands[0].primitive_slots.len(), 1);
}

#[test]
fn grouped_provider_declarators_select_each_array_and_keep_sibling_writes_visible() {
    let consumer = nonempty_consumer();
    let grouped_provider =
        "var first = ['a'], second = ['b'];\nexport { first as left, second as right };\n";
    let fixture = run_configured(
        &consumer,
        grouped_provider,
        nonempty_consumer_modules(),
        input(
            &consumer,
            grouped_provider,
            None,
            None,
            "var values = ['a'], sibling = ['b'];",
            "var sibling = ['a'], values = ['b'];",
        ),
    );
    assert_preserved_entry_output(&fixture, "a b true 2 2\n");

    let mutation_consumer = empty_consumer();
    let sibling_write_provider = "var first = ['a'], second = ['b'], sibling = (first[0] = 'changed');\nexport { first as left, second as right };\n";
    let contract = input(
        &mutation_consumer,
        sibling_write_provider,
        None,
        None,
        "var values = ['a'], other = ['b'], sibling = (values[0] = 'changed');",
        "var other = ['a'], values = ['b'], sibling = (other[0] = 'changed');",
    );
    let rejected = run_rejected(&mutation_consumer, sibling_write_provider, Some(contract));
    assert!(
        rejected
            .stderr
            .contains("known indexed/member write in provider"),
        "the selected array's write in a sibling declarator must remain visible:\n{}",
        rejected.stderr
    );
}

#[test]
fn duplicate_provider_declarators_in_one_grouped_item_reject_ambiguity() {
    let consumer = empty_consumer();
    let grouped_duplicate = "var first = ['a'], first = ['b'];\nconst second = ['b'];\nexport { first as left, second as right };\n";
    let contract = input(
        &consumer,
        grouped_duplicate,
        None,
        None,
        "var values = ['a'], ANYTHING = ANYTHING;",
        "const values = ['b'];",
    );
    let rejected = run_rejected(&consumer, grouped_duplicate, Some(contract));
    assert!(
        rejected
            .stderr
            .contains("has duplicate simple declarators for its selected binding"),
        "the selected body item must reject duplicate simple declarators for the provider binding:\n{}",
        rejected.stderr
    );
}

#[test]
fn destructuring_sibling_cannot_rebind_the_selected_provider_binding() {
    let consumer = empty_consumer();
    let provider = "var first = ['a'], { first } = { first: ['changed'] };\nconst second = ['b'];\nexport { first as left, second as right };\n";
    let contract = input(
        &consumer,
        provider,
        None,
        None,
        "var values = ['a'], DECLARATORS_AFTER = null;",
        "const values = ['b'];",
    );
    let rejected = run_rejected_without_provider_ownership(&consumer, provider, contract);
    assert!(
        rejected
            .stderr
            .contains("has another declarator that defines its selected binding"),
        "a destructuring sibling rebinding the selected name must be rejected:\n{}",
        rejected.stderr
    );
}

#[test]
fn cross_statement_var_reinitialization_of_provider_array_is_rejected() {
    let consumer = empty_consumer();
    let provider = "var first = ['a'];\nvar first = ['changed'];\nconst second = ['b'];\nexport { first as left, second as right };\n";
    let contract = input(
        &consumer,
        provider,
        None,
        None,
        "var values = ['a'];",
        "const values = ['b'];",
    );
    let rejected = run_rejected_without_provider_ownership(&consumer, provider, contract);
    assert!(
        rejected.stderr.contains("known source rebind in provider"),
        "a second initialized var declaration must be surfaced as a known rebind:\n{}",
        rejected.stderr
    );
}

#[test]
fn producer_holes_and_nonprimitive_values_are_rejected() {
    for (provider, pattern, expected) in [
        (
            "var first = [,];\nconst second = [];\nexport { first as left, second as right };\n",
            "var values = [,];",
            "contains a hole",
        ),
        (
            "var first = [{}];\nconst second = [];\nexport { first as left, second as right };\n",
            "var values = [{}];",
            "only primitive literals",
        ),
    ] {
        let consumer = empty_consumer();
        let contract = input(
            &consumer,
            provider,
            None,
            None,
            pattern,
            "const values = [];",
        );
        let rejected = run_rejected(&consumer, provider, Some(contract));
        assert!(rejected.stderr.contains(expected), "{}", rejected.stderr);
    }
}

#[test]
fn consumer_provider_digest_and_import_identity_are_exact() {
    let consumer = empty_consumer();
    let mut changed_consumer = consumer.clone();
    changed_consumer.push_str("// changed source digest\n");
    let stale_consumer = input(
        &changed_consumer,
        EMPTY_PROVIDER,
        Some(&consumer),
        None,
        "var values = [];",
        "const values = [];",
    );
    let rejected = run_rejected(&changed_consumer, EMPTY_PROVIDER, Some(stale_consumer));
    assert!(
        rejected.stderr.contains("stale audited_source_sha256"),
        "{}",
        rejected.stderr
    );

    let changed_provider = EMPTY_PROVIDER.replace(
        "const second = [];",
        "const second = [];\n// changed provider",
    );
    let stale_provider = input(
        &consumer,
        &changed_provider,
        None,
        Some(EMPTY_PROVIDER),
        "var values = [];",
        "const values = [];",
    );
    let rejected = run_rejected(&consumer, &changed_provider, Some(stale_provider));
    assert!(
        rejected
            .stderr
            .contains("stale audited_provider_source_sha256"),
        "{}",
        rejected.stderr
    );

    let drifted_import = consumer.replace("left as inputLeft", "right as inputLeft");
    let identity_drift = input(
        &drifted_import,
        EMPTY_PROVIDER,
        None,
        None,
        "var values = [];",
        "const values = [];",
    );
    let rejected = run_rejected(&drifted_import, EMPTY_PROVIDER, Some(identity_drift));
    assert!(
        rejected
            .stderr
            .contains("does not resolve to the selected producer declaration"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn known_protocol_and_local_mutation_facts_reject_the_scoped_premise() {
    let consumer = empty_consumer();
    for mutation in [
        "Array.prototype[Symbol.iterator] = function* () { yield 'changed'; };\n",
        "Object.getPrototypeOf([][Symbol.iterator]()).next = function () { return { done: true }; };\n",
        "Object.setPrototypeOf(first, { [Symbol.iterator]: function* () { yield 'changed'; } });\n",
    ] {
        let provider = format!(
            "{mutation}var first = [];\nconst second = [];\nexport {{ first as left, second as right }};\n"
        );
        let contract = input(
            &consumer,
            &provider,
            None,
            None,
            "var values = [];",
            "const values = [];",
        );
        let rejected = run_rejected(&consumer, &provider, Some(contract));
        assert!(
            rejected
                .stderr
                .contains("known Array iterator protocol write")
                || rejected.stderr.contains("indexed/member write in provider"),
            "mutation {mutation:?} was not rejected as a known contradiction:\n{}",
            rejected.stderr
        );
    }

    let provider_mutations = [
        "var first = ['a'];\nfirst[0] = 'changed';\nconst second = ['b'];\nexport { first as left, second as right };\n",
        "var first = ['a'];\nfirst = ['changed'];\nconst second = ['b'];\nexport { first as left, second as right };\n",
    ];
    for provider in provider_mutations {
        let contract = input(
            &consumer,
            provider,
            None,
            None,
            "var values = ['a'];",
            "const values = ['b'];",
        );
        let rejected = run_rejected(&consumer, provider, Some(contract));
        assert!(
            rejected.stderr.contains("known source rebind")
                || rejected.stderr.contains("known indexed/member write"),
            "provider mutation was not rejected:\n{}",
            rejected.stderr
        );
    }

    let consumer_mutation = consumer.replacen(
        CONSUMER_SPREAD,
        "inputLeft[0] = 'changed';\nconst merged = [...inputLeft, ...inputRight];\n",
        1,
    );
    let contract = input(
        &consumer_mutation,
        EMPTY_PROVIDER,
        None,
        None,
        "var values = [];",
        "const values = [];",
    );
    let rejected = run_rejected(&consumer_mutation, EMPTY_PROVIDER, Some(contract));
    assert!(
        rejected.stderr.contains(
            "unsupported: imported array binding inputLeft has a known indexed/member write"
        ),
        "consumer mutation was not rejected:\n{}",
        rejected.stderr
    );
}

#[test]
fn circular_provider_initialization_is_rejected() {
    let consumer = empty_consumer();
    let provider = "import './app.js';\nvar first = [];\nconst second = [];\nexport { first as left, second as right };\n";
    let contract = input(
        &consumer,
        provider,
        None,
        None,
        "var values = [];",
        "const values = [];",
    );
    let rejected = run_rejected(&consumer, provider, Some(contract));
    assert!(
        rejected
            .stderr
            .contains("complete acyclic ordinary static initialization closure"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn admitted_direct_and_transitive_node_builtin_dependencies_preserve_runtime() {
    let consumer = nonempty_consumer();
    let provider = format!(
        "import 'node:path';\nimport {{ marker }} from './helper.js';\n\
         if (marker !== 1) throw new Error('helper did not initialize');\n\
         {NONEMPTY_PROVIDER}"
    );
    let provider_guard = "if (marker !== 1) throw new Error('helper did not initialize');";
    let helper = "import 'node:util';\nexport const marker = 1;\n";
    let contract = input(
        &consumer,
        &provider,
        None,
        None,
        "var values = ['a'];",
        "const values = ['b'];",
    );

    let chunks = [
        ("static/provider", provider.as_str()),
        ("static/helper", helper),
    ];
    let provider_logical_modules = [
        (
            "static/provider",
            provider_modules_with_guard(provider_guard),
        ),
        (
            "static/helper",
            vec![logical_module("values/helper", &[Member::new("marker")])],
        ),
    ];
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(&consumer, nonempty_consumer_modules())
            .with_extra_chunks(&chunks)
            .with_extra_chunk_logical_modules(&provider_logical_modules)
            .with_imported_primitive_array_spread_inputs("static/app", vec![contract])
            .with_host_behavior(admitted_builtin_modules(&[
                NodeBuiltinModule::Path,
                NodeBuiltinModule::Util,
            ])),
    );
    assert_preserved_entry_output(&fixture, "a b true 2 2\n");
}

#[test]
fn builtin_dependency_closure_requires_each_builtin_to_be_admitted() {
    let consumer = nonempty_consumer();
    let provider = format!(
        "import 'path';\nimport {{ marker }} from './helper.js';\n\
         if (marker !== 1) throw new Error('helper did not initialize');\n\
         {NONEMPTY_PROVIDER}"
    );
    let helper = "import 'node:util';\nexport const marker = 1;\n";
    let contract = input(
        &consumer,
        &provider,
        None,
        None,
        "var values = ['a'];",
        "const values = ['b'];",
    );

    let rejected = run_rejected_builtin_closure_fixture(
        &consumer,
        &provider,
        helper,
        contract,
        HostBehavior::default(),
    );
    assert!(
        rejected
            .stderr
            .contains("complete acyclic ordinary static initialization closure"),
        "without builtin admission, the imported array closure stays incomplete:\n{}",
        rejected.stderr
    );
}

#[test]
fn unlisted_builtins_and_unknown_bare_imports_remain_unresolved() {
    let consumer = nonempty_consumer();
    let provider = format!(
        "import 'path';\nimport {{ marker }} from './helper.js';\n\
         if (marker !== 1) throw new Error('helper did not initialize');\n\
         {NONEMPTY_PROVIDER}"
    );
    let host = admitted_builtin_modules(&[NodeBuiltinModule::Path, NodeBuiltinModule::Util]);

    for (helper, request) in [
        (
            "import 'node:util';\nimport 'node:fs';\nexport const marker = 1;\n",
            "unlisted Node builtins",
        ),
        (
            "import 'node:util';\nimport '@fixture/not-a-node-builtin';\nexport const marker = 1;\n",
            "unknown bare packages",
        ),
    ] {
        let contract = input(
            &consumer,
            &provider,
            None,
            None,
            "var values = ['a'];",
            "const values = ['b'];",
        );
        let rejected = run_rejected_builtin_closure_fixture(
            &consumer,
            &provider,
            helper,
            contract,
            host.clone(),
        );
        assert!(
            rejected
                .stderr
                .contains("complete acyclic ordinary static initialization closure"),
            "{request} must not be treated as admitted builtin leaves:\n{}",
            rejected.stderr
        );
    }
}
