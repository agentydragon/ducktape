//! Exact, source-bound inputs let one imported primitive-array spread retain
//! native yields while keeping provider ownership and caller effects visible.

use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use sha2::{Digest, Sha256};
use spec::{
    ImportedPrimitiveArrayLifetimeContract, ImportedPrimitiveArrayOperandContract,
    ImportedPrimitiveArraySpreadInputContract, SourceMatchClaim,
};

const EMPTY_PROVIDER: &str =
    "var first = [];\nconst second = [];\nexport { first as left, second as right };\n";
const NONEMPTY_PROVIDER: &str =
    "var first = [\"a\"];\nconst second = [\"b\"];\nexport { first as left, second as right };\n";
const CONSUMER_IMPORTS: &str =
    "import { left as inputLeft, right as inputRight } from './provider.js';\n";
const CONSUMER_SPREAD: &str = "const merged = [...inputLeft, ...inputRight];\n";
const CONSUMER_SELECTOR: &str = "const result = [...left, ...right];";

fn sha256(source: &str) -> String {
    Sha256::digest(source.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn source_match(pattern: &str, binding: &str) -> SourceMatchClaim {
    serde_yaml::from_str(&format!(
        "match: {:?}\nbindings: [{binding}]\n",
        pattern.trim()
    ))
    .expect("valid source-match claim")
}

fn input(
    consumer: &str,
    provider: &str,
    consumer_digest: Option<&str>,
    provider_digest: Option<&str>,
    first_provider_pattern: &str,
    second_provider_pattern: &str,
) -> ImportedPrimitiveArraySpreadInputContract {
    ImportedPrimitiveArraySpreadInputContract {
        source_match: source_match(CONSUMER_SELECTOR, "result"),
        operands: vec![
            ImportedPrimitiveArrayOperandContract {
                selector_local: "left".to_string(),
                provider_chunk: "static/provider".to_string(),
                provider_source_match: source_match(first_provider_pattern, "values"),
                audited_provider_source_sha256: sha256(provider_digest.unwrap_or(provider)),
            },
            ImportedPrimitiveArrayOperandContract {
                selector_local: "right".to_string(),
                provider_chunk: "static/provider".to_string(),
                provider_source_match: source_match(second_provider_pattern, "values"),
                audited_provider_source_sha256: sha256(provider_digest.unwrap_or(provider)),
            },
        ],
        audited_source_sha256: sha256(consumer_digest.unwrap_or(consumer)),
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

fn run_configured(
    consumer: &str,
    provider: &str,
    consumer_logical_modules: Vec<LogicalModuleEntry>,
    contract: ImportedPrimitiveArraySpreadInputContract,
) -> Fixture {
    let chunks = [("static/provider", provider)];
    let provider_logical_modules = [("static/provider", provider_modules())];
    run_fixture(
        FixtureOpts::new(consumer, consumer_logical_modules)
            .with_extra_chunks(&chunks)
            .with_extra_chunk_logical_modules(&provider_logical_modules)
            .with_imported_primitive_array_spread_inputs("static/app", vec![contract]),
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
    run_rejection_fixture(match contract {
        Some(contract) => {
            opts.with_imported_primitive_array_spread_inputs("static/app", vec![contract])
        }
        None => opts,
    })
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
fn empty_imported_spread_breaks_the_callback_read_cycle_and_preserves_unrelated_callback() {
    let consumer = empty_consumer();
    let base = run_rejected(&consumer, EMPTY_PROVIDER, None);
    assert!(
        base.cycles().iter().any(|cycle| {
            cycle["modules"]
                .as_array()
                .is_some_and(|modules| modules.iter().any(|module| module == "values/later"))
        }),
        "the unproved imported spread should retain the callback/later cycle: {:#?}",
        base.cycles()
    );

    let fixture = run_configured(
        &consumer,
        EMPTY_PROVIDER,
        empty_consumer_modules(),
        input(
            &consumer,
            EMPTY_PROVIDER,
            None,
            None,
            "var values = [];",
            "const values = [];",
        ),
    );
    assert_preserved_entry_output(&fixture, "0 false 0 1 0 true\n");
    assert_module_exports(
        &fixture.out_root,
        "static/provider/modules/arrays/provider.js",
        &["left", "right"],
        &[],
    );
    let graph = fixture.dependency_graph();
    let merged = source_unit_for_binding(&graph, "merged");
    let merged_node = graph.nodes.iter().find(|node| node.id == merged).unwrap();
    let execution = merged_node
        .synchronous_execution
        .as_ref()
        .expect("spread execution evidence");
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
