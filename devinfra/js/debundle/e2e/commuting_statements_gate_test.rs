//! What an accepted `commuting_statements` pair buys the realizability gate,
//! and the source facts that reject one (SPEC.md § Commuting statements):
//!
//! - a `sequenced`-only cycle between two modules is cleared by asserting the
//!   pair, and the emitted bundle preserves the original entry output;
//! - `eager_use` edges are never inerted, so an eager cycle still blocks;
//! - a pair the analysis derived to conflict (a known write and read of one
//!   cell or binding, a shared `var`) is rejected even though asserted;
//! - a pair with no `sequenced` edge between its endpoints is rejected as
//!   unused.

use analysis::analysis_inputs::CommutingStatementsRef;
use analysis::{DepKind, DependencyGraphReport};
use debundle_e2e_support::*;

/// `globalThis.record` is a callable the analysis cannot see into, so every
/// call to it is an opaque effect ordered only conservatively.
const OPAQUE_SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nconst alpha = (globalThis.record(\"alpha\"), \"A\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nconst gamma = (globalThis.record(\"gamma\"), \"C\");\nconsole.log(alpha, beta, gamma, trace.slice().sort().join(\",\"));\nexport { alpha, beta, gamma };\n";
const RECORD_INSTALL: &str = "globalThis.record = (key) => { trace.push(key); };";
const OPAQUE_OUTPUT: &str = "A B C alpha,beta,gamma\n";
const ENTRY: &str = "commuting_statements[0]";
const CONTRADICTION: &str = "contradicts a source fact";

/// `outer` holds `alpha` and `gamma`, `inner` holds `beta`: the strict S-chain
/// `beta -> alpha` and `gamma -> beta` form a `sequenced`-only cycle between
/// the two modules.
fn opaque_split(pairs: Vec<CommutingStatementsContract>) -> FixtureOpts<'static> {
    FixtureOpts::new(
        OPAQUE_SOURCE,
        vec![
            logical_module_with_anon(
                "probes/outer",
                &[
                    Member::new("trace"),
                    Member::new("alpha"),
                    Member::new("gamma"),
                ],
                &[RECORD_INSTALL],
            ),
            logical_module("probes/inner", &[Member::new("beta")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_commuting_statements("static/app", pairs)
}

fn beta_gamma_pair() -> CommutingStatementsContract {
    commuting_pair(
        member_source_unit("probes/inner", "beta"),
        member_source_unit("probes/outer", "gamma"),
    )
}

fn premise(input_index: usize) -> CommutingStatementsRef {
    CommutingStatementsRef {
        defining_chunk: "static/app".to_owned(),
        input_index,
    }
}

/// Both `spec validate` and `run` reject the spec with a diagnostic carrying
/// every token.
fn expect_rejected<'a>(build: impl Fn() -> FixtureOpts<'a>, tokens: &[&str]) {
    let fixture = write_validate_fixture_spec(build());
    let validate = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(
        !validate.status.success(),
        "spec validate accepted the spec; stdout={}",
        validate.stdout
    );
    for token in tokens {
        assert!(
            validate.stderr.contains(token),
            "spec validate stderr missing {token:?}:\n{}",
            validate.stderr
        );
    }
    expect_rejection_containing_all(build(), tokens);
}

/// The inert edges a premise bought, as recorded in `dependency_graph.json`.
fn inerted_edges<'a>(
    graph: &'a DependencyGraphReport,
    premise: &CommutingStatementsRef,
) -> Vec<&'a analysis::DependencyEdgeReport> {
    let entry = graph
        .commuting_statements
        .iter()
        .find(|entry| entry.reference == *premise)
        .unwrap_or_else(|| panic!("no applied premise {premise:?}: {graph:#?}"));
    entry
        .inerted_edge_ids
        .iter()
        .map(|id| {
            graph
                .edges
                .iter()
                .find(|edge| edge.id == *id)
                .unwrap_or_else(|| panic!("inerted edge {id} missing from edges"))
        })
        .collect()
}

#[test]
fn sequenced_only_cycle_is_cleared_by_the_asserted_pair() {
    // Positive anchor: the same split without the premise is a cycle.
    expect_cycle_rejection(opaque_split(vec![]), &["probes/outer", "probes/inner"]);

    let fixture = run_fixture(opaque_split(vec![beta_gamma_pair()]));
    assert_preserved_entry_output(&fixture, OPAQUE_OUTPUT);

    let graph = fixture.dependency_graph();
    let beta = source_unit_for_binding(&graph, "beta");
    let gamma = source_unit_for_binding(&graph, "gamma");
    let inerted = inerted_edges(&graph, &premise(0));
    assert!(!inerted.is_empty(), "{graph:#?}");
    for edge in &inerted {
        assert_eq!(edge.edge_kind, DepKind::Sequenced);
        assert!(!edge.constrains_init_order, "{edge:?}");
        assert_eq!(edge.commuting_premise, Some(premise(0)));
        assert!(
            (edge.source == gamma && edge.target == beta)
                || (edge.source == beta && edge.target == gamma),
            "inerted edge outside the pair: {edge:?}"
        );
    }
    assert!(
        graph
            .edges
            .iter()
            .filter(|edge| edge.commuting_premise.is_some())
            .all(|edge| inerted.contains(&edge)),
        "an edge is inert without being recorded on its premise: {graph:#?}"
    );
    let [entry] = graph.commuting_statements.as_slice() else {
        panic!("{:#?}", graph.commuting_statements);
    };
    assert_eq!(entry.a, beta);
    assert_eq!(entry.b, gamma);
    assert!(entry.spec_location.contains(ENTRY), "{entry:?}");
}

/// Two summarized effects on disjoint cells under the strict S-chain are
/// ordered only by adjacency: the analysis can already decide the pair, so
/// the premise certifies nothing and is rejected as unused (the remedy is
/// `dataflow_aware_s_chain`, under which no edge joins them).
#[test]
fn summarized_disjoint_endpoints_under_strict_chain_are_unused() {
    const SOURCE: &str = "const alpha = (globalThis.alpha = \"a\", \"A\");\nconst beta = (globalThis.beta = \"b\", \"B\");\nconst gamma = (globalThis.gamma = \"c\", \"C\");\nconsole.log(alpha, beta, gamma, globalThis.alpha, globalThis.beta, globalThis.gamma);\nexport { alpha, beta, gamma };\n";
    let split = |pairs| {
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module("slots/outer", &[Member::new("alpha"), Member::new("gamma")]),
                logical_module("slots/inner", &[Member::new("beta")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements("static/app", pairs)
    };
    // Positive anchor: the strict chain does order the pair into a cycle.
    expect_cycle_rejection(split(vec![]), &["slots/outer", "slots/inner"]);
    expect_rejected(
        || {
            split(vec![commuting_pair(
                member_source_unit("slots/inner", "beta"),
                member_source_unit("slots/outer", "gamma"),
            )])
        },
        &[ENTRY, "inerts no sequenced edge", "dataflow_aware_s_chain"],
    );
}

#[test]
fn eager_use_cycle_is_not_cleared_by_an_accepted_pair() {
    const SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nconst alpha = (globalThis.record(\"alpha\"), \"A\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nconst gamma = beta.length;\nconst delta = gamma + 1;\nconsole.log(alpha, beta, gamma, delta, trace.slice().sort().join(\",\"));\nexport { alpha, beta, gamma, delta };\n";
    let split = |pairs| {
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "probes/outer",
                    &[
                        Member::new("trace"),
                        Member::new("alpha"),
                        Member::new("gamma"),
                    ],
                    &[RECORD_INSTALL],
                ),
                logical_module("probes/inner", &[Member::new("beta"), Member::new("delta")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements("static/app", pairs)
    };
    let modules = ["probes/outer", "probes/inner"];
    let eager_cut = |scc: &serde_json::Value| -> Vec<serde_json::Value> {
        scc["cut"]
            .as_array()
            .expect("cut")
            .iter()
            .filter(|edge| edge["kind"] == "eager_use")
            .cloned()
            .collect()
    };

    let without = expect_cycle_rejection(split(vec![]), &modules);
    let with = expect_cycle_rejection(
        split(vec![commuting_pair(
            member_source_unit("probes/outer", "alpha"),
            member_source_unit("probes/inner", "beta"),
        )]),
        &modules,
    );
    // The premise inerted `beta -> alpha`, and only that; the at-init reads
    // `gamma -> beta` / `delta -> gamma` still close the SCC.
    assert_eq!(with["modules"], without["modules"]);
    assert_eq!(
        eager_cut(&with),
        eager_cut(&without),
        "{with:#}\n{without:#}"
    );
    assert!(!eager_cut(&with).is_empty(), "{with:#}");
    assert!(
        with["cut"]
            .as_array()
            .expect("cut")
            .iter()
            .all(|edge| edge["commuting_premise"].is_null()),
        "an inert edge appears in the cut: {with:#}"
    );
    let inert = with["inert_commuting_edges"]
        .as_array()
        .expect("inert_commuting_edges");
    assert_eq!(inert.len(), 1, "{with:#}");
    assert_eq!(inert[0]["kind"], "sequenced");
    assert_eq!(inert[0]["from"], "probes/inner");
    assert_eq!(inert[0]["to"], "probes/outer");
    assert_eq!(
        inert[0]["commuting_premise"],
        serde_json::to_value(premise(0)).unwrap()
    );
    assert!(without["inert_commuting_edges"].is_null(), "{without:#}");
}

/// A known write and read of one `globalThis` property under the
/// dataflow-aware S-chain is a derived conflict; the author's assertion does
/// not override it.
#[test]
fn known_global_property_conflict_rejects_the_pair() {
    const SOURCE: &str = "globalThis.flag = \"first\";\nconst snapshot = globalThis.flag;\nconsole.log(snapshot);\nexport { snapshot };\n";
    expect_rejected(
        || {
            FixtureOpts::new(
                SOURCE,
                vec![
                    logical_module_with_anon("mod_w", &[], &["globalThis.flag = \"first\";"]),
                    logical_module("mod_read", &[Member::new("snapshot")]),
                ],
            )
            .with_dataflow_aware_s_chain()
            .with_commuting_statements(
                "static/app",
                vec![commuting_pair(
                    anonymous_source_unit("globalThis.flag = \"first\";"),
                    member_source_unit("mod_read", "snapshot"),
                )],
            )
        },
        &[ENTRY, CONTRADICTION, "`snapshot`", "global property `flag`"],
    );
}

/// A binding's write/read timeline is a derived conflict in either S-chain
/// mode.
#[test]
fn known_binding_write_after_read_rejects_the_pair() {
    const SOURCE: &str = "let count = 0;\nconst snap = count;\ncount = 1;\nconsole.log(snap, count);\nexport { count, snap };\n";
    expect_rejected(
        || {
            FixtureOpts::new(
                SOURCE,
                vec![logical_module_with_anon(
                    "all",
                    &[Member::new("count"), Member::new("snap")],
                    &["count = 1;"],
                )],
            )
            .with_unassigned_mode(unassigned_mode_inline())
            .with_commuting_statements(
                "static/app",
                vec![commuting_pair(
                    member_source_unit("all", "snap"),
                    anonymous_source_unit("count = 1;"),
                )],
            )
        },
        &[ENTRY, CONTRADICTION, "`snap`", "timeline of `count`"],
    );
}

const SHARED_VAR_SOURCE: &str = "var shared = 1;\nconst snap = shared;\nvar shared = 2;\nconsole.log(snap, shared);\nexport { shared, snap };\n";

fn shared_var_split(pair: CommutingStatementsContract) -> FixtureOpts<'static> {
    FixtureOpts::new(
        SHARED_VAR_SOURCE,
        vec![logical_module(
            "all",
            &[Member::new("shared"), Member::new("snap")],
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_commuting_statements("static/app", vec![pair])
}

/// Accesses of a binding declared at several `var` sites keep source order
/// even when pure; the premise cannot reorder a reader against an
/// initializer.
#[test]
fn shared_var_access_order_is_not_inertable() {
    expect_rejected(
        || {
            shared_var_split(commuting_pair(
                member_source_unit("all", "snap"),
                declared_source_unit("var shared = 1;", "shared"),
            ))
        },
        &[ENTRY, CONTRADICTION, "`snap`", "`shared`"],
    );
}

/// Each declaration site of a repeated `var` is its own source unit, selected
/// by the template that matches it; a pair of two sites is rejected because
/// they co-declare one binding (regression: the second site used to resolve
/// to the first, making the pair a same-source-unit error).
#[test]
fn co_declaration_sites_of_a_repeated_var_reject_the_pair() {
    expect_rejected(
        || {
            shared_var_split(commuting_pair(
                declared_source_unit("var shared = 1;", "shared"),
                declared_source_unit("var shared = 2;", "shared"),
            ))
        },
        &[ENTRY, CONTRADICTION, "shared `var` `shared`"],
    );
}

/// `alpha` and `gamma` are not adjacent in the strict S-chain, so no edge
/// joins them: the premise buys nothing and fails validation.
#[test]
fn pair_without_a_sequenced_edge_is_unused() {
    expect_rejected(
        || {
            opaque_split(vec![commuting_pair(
                member_source_unit("probes/outer", "alpha"),
                member_source_unit("probes/outer", "gamma"),
            )])
        },
        &[ENTRY, "inerts no sequenced edge", "`alpha`", "`gamma`"],
    );
}
