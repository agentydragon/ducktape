//! What an accepted `commuting_statements` pair buys the realizability gate,
//! and the source facts that reject one (SPEC.md § Commuting statements):
//!
//! - a `sequenced`-only cycle between two modules is cleared by asserting the
//!   pair, and the emitted bundle preserves the original entry output;
//! - `eager_use` edges are never inerted, so an eager cycle still blocks;
//! - a pair the analysis derived to conflict (a known write and read of one
//!   cell or binding, a shared `var`) is rejected even though asserted;
//! - a binding timeline is a derived conflict only between two observed
//!   effects; one a conservative path merely assumed (the fallback summary,
//!   a callback opaque code may invoke, an escaped closure, whole-body
//!   promotion) is inertable;
//! - a pair with no `sequenced` edge between its endpoints is unused: a
//!   warning naming the entry by authored identity, never a failure, and the
//!   run emits exactly what it emits without the entry.

use analysis::analysis_inputs::CommutingStatementsRef;
use analysis::graph::UnusedPremiseReason;
use analysis::{CommutingStatementsDiagnostic, DependencyGraphReport, SequencedCause};
use analysis_model::DepKind;
use debundle_e2e_graph_support::{
    FixtureGraphExt, commuting_statements_diagnostics, source_unit_for_binding,
};
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

/// `spec validate` reports the contradicted premise as one typed
/// `commuting_statements_diagnostics` entry naming the entry and fails
/// validation; `run` rejects the spec with a diagnostic carrying every token.
fn expect_contradicted<'a>(build: impl Fn() -> FixtureOpts<'a>, tokens: &[&str]) {
    let fixture = write_validate_fixture_spec(build());
    let validate = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    let report: serde_json::Value = serde_json::from_str(&validate.stdout)
        .unwrap_or_else(|error| panic!("{error}: spec validate stdout={}", validate.stdout));
    assert_eq!(report["validation"]["success"], false, "{report:#}");
    let [diagnostic] = report["commuting_statements_diagnostics"]
        .as_array()
        .expect("commuting_statements_diagnostics")
        .as_slice()
    else {
        panic!("{report:#}");
    };
    assert_eq!(diagnostic["kind"], "contradicted", "{report:#}");
    assert!(
        diagnostic["spec_location"]
            .as_str()
            .is_some_and(|location| location.contains(ENTRY)),
        "{report:#}"
    );
    expect_rejection_containing_all(build(), tokens);
}

/// One authored endpoint expected on an unused-premise warning.
#[derive(Clone, Copy)]
struct ExpectedEndpoint {
    module: &'static str,
    name: &'static str,
}

/// The single unused-premise warning in `diagnostics`, checked against the
/// authored identity of `entry` and its endpoints.
fn assert_unused_warning(
    diagnostics: &[CommutingStatementsDiagnostic],
    entry: &str,
    reason: UnusedPremiseReason,
    endpoints: [ExpectedEndpoint; 2],
) {
    let [CommutingStatementsDiagnostic::Unused(unused)] = diagnostics else {
        panic!("expected one unused warning: {diagnostics:#?}");
    };
    assert!(unused.spec_location.ends_with(entry), "{unused:#?}");
    assert_eq!(unused.reason, reason, "{unused:#?}");
    for (endpoint, expected) in [&unused.a, &unused.b].into_iter().zip(endpoints) {
        let [member] = endpoint.logical_members.as_slice() else {
            panic!("expected one authored member: {unused:#?}");
        };
        assert_eq!(
            (member.module.as_str(), member.name.as_str()),
            (expected.module, expected.name),
            "{unused:#?}"
        );
        assert!(endpoint.source_location.is_some(), "{unused:#?}");
    }
}

/// An unused entry is a warning: with `unused` as the only entry, `run`
/// succeeds and emits exactly the tree it emits without it (both under Node
/// parity), the chunk report carries the typed warning, and `spec validate`
/// succeeds while listing it.
fn expect_unused_is_a_warning(
    build: impl Fn(Vec<CommutingStatementsContract>) -> FixtureOpts<'static>,
    unused: CommutingStatementsContract,
    expected_output: &str,
    expected: (&str, UnusedPremiseReason, [ExpectedEndpoint; 2]),
) {
    let (entry, reason, endpoints) = expected;
    let baseline = run_fixture(build(vec![]));
    let with_entry = run_fixture(build(vec![unused.clone()]));
    assert_preserved_entry_output(&with_entry, expected_output);
    assert_same_tree(&baseline.out_root, &with_entry.out_root);
    assert_unused_warning(
        &commuting_statements_diagnostics(&with_entry.report_root, &with_entry.chunk_id),
        entry,
        reason,
        endpoints,
    );

    let fixture = write_validate_fixture_spec(build(vec![unused]));
    let validate = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    let report: serde_json::Value = serde_json::from_str(&validate.stdout)
        .unwrap_or_else(|error| panic!("{error}: spec validate stdout={}", validate.stdout));
    assert_eq!(report["validation"]["success"], true, "{report:#}");
    let diagnostics: Vec<CommutingStatementsDiagnostic> =
        serde_json::from_value(report["commuting_statements_diagnostics"].clone())
            .unwrap_or_else(|error| panic!("{error}: {report:#}"));
    assert_unused_warning(&diagnostics, entry, reason, endpoints);
}

/// The inert edges a premise bought, as recorded in `dependency_graph.json.gz`.
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

fn heap_frontier_split(
    source: &'static str,
    observer_members: &[&'static str],
    mutator_members: &[&'static str],
    pairs: Vec<CommutingStatementsContract>,
) -> FixtureOpts<'static> {
    let observer_members: Vec<_> = observer_members
        .iter()
        .map(|name| Member::new(name))
        .collect();
    let mutator_members: Vec<_> = mutator_members
        .iter()
        .map(|name| Member::new(name))
        .collect();
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "provider",
                &[
                    Member::new("store"),
                    Member::new("read"),
                    Member::new("write"),
                    Member::new("noop"),
                ],
            ),
            logical_module("observer", &observer_members),
            logical_module("mutator", &mutator_members),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_dataflow_aware_s_chain()
    .with_trusted_dataflow_summaries()
    .with_commuting_statements("static/app", pairs)
}

/// Commuting an observer past a no-op must not move it past the later writer
/// whose ordering was represented through the no-op's heap frontier.
#[test]
fn commuting_a_heap_frontier_preserves_third_statement_order() {
    const SOURCE: &str = "const store = [];\nfunction read() { return store.length; }\nfunction write() { store.push(1); }\nfunction noop() {}\nconst anchor = 0;\nconst gamma = read();\nconst beta = noop?.();\nconst alpha = write();\nconsole.log(gamma, beta, alpha, store.length);\n";
    let fixture = run_fixture(heap_frontier_split(
        SOURCE,
        &["gamma"],
        &["anchor", "beta", "alpha"],
        vec![commuting_pair(
            member_source_unit("mutator", "beta"),
            member_source_unit("observer", "gamma"),
        )],
    ));
    assert_preserved_entry_output(&fixture, "0 undefined undefined 1\n");
    let graph = fixture.dependency_graph();
    let alpha = source_unit_for_binding(&graph, "alpha");
    let gamma = source_unit_for_binding(&graph, "gamma");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == alpha
                && edge.target == gamma
                && edge.constrains_init_order
                && matches!(
                    edge.sequenced_cause,
                    Some(SequencedCause::PreservedOrder { .. })
                )
        }),
        "{graph:#?}"
    );
}

#[test]
fn commuting_a_writer_past_a_frontier_preserves_the_earlier_observer() {
    const SOURCE: &str = "const store = [];\nfunction read() { return store.length; }\nfunction write() { store.push(1); }\nfunction noop() {}\nconst anchor = 0;\nconst gamma = read();\nconst beta = noop?.();\nconst alpha = write();\nconsole.log(gamma, beta, alpha, store.length);\n";
    let fixture = run_fixture(heap_frontier_split(
        SOURCE,
        &["gamma", "beta"],
        &["anchor", "alpha"],
        vec![commuting_pair(
            member_source_unit("mutator", "alpha"),
            member_source_unit("observer", "beta"),
        )],
    ));
    assert_preserved_entry_output(&fixture, "0 undefined undefined 1\n");
}

#[test]
fn overlapping_heap_commutations_preserve_order_independently_of_spec_order() {
    const SOURCE: &str = "const store = [];\nfunction read() { return store.length; }\nfunction write() { store.push(1); }\nfunction noop() {}\nconst anchor = 0;\nconst gamma = read();\nconst beta = noop?.();\nconst delta = noop?.();\nconst alpha = write();\nconsole.log(gamma, beta, delta, alpha, store.length);\n";
    let pairs = vec![
        commuting_pair(
            member_source_unit("mutator", "beta"),
            member_source_unit("observer", "gamma"),
        ),
        commuting_pair(
            member_source_unit("mutator", "delta"),
            member_source_unit("mutator", "beta"),
        ),
        commuting_pair(
            member_source_unit("mutator", "delta"),
            member_source_unit("observer", "gamma"),
        ),
    ];
    for pairs in [pairs.clone(), pairs.into_iter().rev().collect()] {
        let fixture = run_fixture(heap_frontier_split(
            SOURCE,
            &["gamma"],
            &["anchor", "beta", "delta", "alpha"],
            pairs,
        ));
        assert_preserved_entry_output(&fixture, "0 undefined undefined undefined 1\n");
        assert!(fixture.dependency_graph().edges.iter().any(|edge| {
            edge.commuting_premise.is_some()
                && matches!(
                    edge.sequenced_cause,
                    Some(SequencedCause::PreservedOrder { .. })
                )
        }));
    }
}

/// Two summarized effects on disjoint cells under the strict S-chain are
/// ordered only by adjacency: the analysis can already decide the pair, so
/// the premise certifies nothing and is reported as unused (the remedy is
/// `dataflow_aware_s_chain`, under which no edge joins them).
const SLOTS_SOURCE: &str = "const alpha = (globalThis.alpha = \"a\", \"A\");\nconst beta = (globalThis.beta = \"b\", \"B\");\nconst gamma = (globalThis.gamma = \"c\", \"C\");\nconsole.log(alpha, beta, gamma, globalThis.alpha, globalThis.beta, globalThis.gamma);\nexport { alpha, beta, gamma };\n";

fn slots_split(
    outer: &[&'static str],
    inner: &[&'static str],
    pairs: Vec<CommutingStatementsContract>,
) -> FixtureOpts<'static> {
    let members =
        |names: &[&'static str]| names.iter().copied().map(Member::new).collect::<Vec<_>>();
    FixtureOpts::new(
        SLOTS_SOURCE,
        vec![
            logical_module("slots/outer", &members(outer)),
            logical_module("slots/inner", &members(inner)),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_commuting_statements("static/app", pairs)
}

fn beta_alpha_slots_pair() -> CommutingStatementsContract {
    commuting_pair(
        member_source_unit("slots/inner", "beta"),
        member_source_unit("slots/outer", "alpha"),
    )
}

#[test]
fn summarized_disjoint_endpoints_under_strict_chain_are_an_unused_warning() {
    expect_unused_is_a_warning(
        |pairs| slots_split(&["alpha"], &["beta", "gamma"], pairs),
        beta_alpha_slots_pair(),
        "A B C a b c\n",
        (
            ENTRY,
            UnusedPremiseReason::SummarizedEndpoints,
            [
                ExpectedEndpoint {
                    module: "slots/inner",
                    name: "beta",
                },
                ExpectedEndpoint {
                    module: "slots/outer",
                    name: "alpha",
                },
            ],
        ),
    );
}

/// The gate rejects the split for a cycle the unused entry does not touch;
/// `--emit-on-rejection` still writes its readable tree and the warning
/// beside the cycle evidence.
#[test]
fn unused_entry_does_not_fail_an_emit_on_rejection_run() {
    // Positive anchor: the strict chain orders the pair into a cycle.
    expect_cycle_rejection(
        slots_split(&["alpha", "gamma"], &["beta"], vec![]),
        &["slots/outer", "slots/inner"],
    );
    let rejected = run_emit_on_rejection_fixture(slots_split(
        &["alpha", "gamma"],
        &["beta"],
        vec![beta_alpha_slots_pair()],
    ));
    assert!(rejected.gate_rejected_marker().is_file());
    assert!(rejected.entry_path().is_file());
    assert!(!rejected.cycles().is_empty());
    assert_unused_warning(
        &commuting_statements_diagnostics(&rejected.report_root, &rejected.chunk_id),
        ENTRY,
        UnusedPremiseReason::SummarizedEndpoints,
        [
            ExpectedEndpoint {
                module: "slots/inner",
                name: "beta",
            },
            ExpectedEndpoint {
                module: "slots/outer",
                name: "alpha",
            },
        ],
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

    let pair = || {
        commuting_pair(
            member_source_unit("probes/outer", "alpha"),
            member_source_unit("probes/inner", "beta"),
        )
    };

    let without = expect_cycle_rejection(split(vec![]), &modules);
    let with = expect_cycle_rejection(split(vec![pair()]), &modules);
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

    // The bail summary and the gate text views name the inert edge (both
    // endpoints) as inert; `describe` lists it apart from the constraining
    // evidence.
    let rejected = run_rejection_fixture(split(vec![pair()]));
    let graph_path = rejected
        .report_root
        .join("static/app/dependency_graph.json.gz");
    let graph_path = graph_path.to_str().expect("utf-8 path");
    let gate_text = |verb: &str| {
        let out = run_debundle(&["gate", verb, "0", "--graph", graph_path, "--format", "text"]);
        assert!(out.status.success(), "gate {verb}: {:?}", out.stderr);
        String::from_utf8(out.stdout).expect("utf-8 output")
    };
    for (view, text) in [
        ("bail summary", rejected.stderr.clone()),
        ("gate describe", gate_text("describe")),
        ("gate cut", gate_text("cut")),
    ] {
        for token in ["inert", "beta (probes/inner)"] {
            assert!(text.contains(token), "{view} lacks {token:?}:\n{text}");
        }
    }
    let described = run_debundle(&[
        "gate", "describe", "0", "--graph", graph_path, "--format", "json",
    ]);
    let described: serde_json::Value =
        serde_json::from_slice(&described.stdout).expect("gate describe JSON");
    assert_eq!(
        described["inert_commuting_edges"], with["inert_commuting_edges"],
        "{described:#}"
    );
    assert!(
        described["evidence"]
            .as_array()
            .expect("evidence")
            .iter()
            .all(|edge| edge["commuting_premise"].is_null()),
        "an inert edge appears in the evidence: {described:#}"
    );
}

/// A known write and read of one `globalThis` property under the
/// dataflow-aware S-chain is a derived conflict; the author's assertion does
/// not override it.
#[test]
fn known_global_property_conflict_rejects_the_pair() {
    const SOURCE: &str = "globalThis.flag = \"first\";\nconst snapshot = globalThis.flag;\nconsole.log(snapshot);\nexport { snapshot };\n";
    expect_contradicted(
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

/// A callee's contents write must reject the premise even when the strict
/// S-chain claimed the pair before the heap timeline could name its cell.
#[test]
fn callee_heap_contents_conflict_rejects_the_pair() {
    const SOURCE: &str = "const cells = [];\nfunction append() { cells.push(1); }\nconst first = (append(), 1);\nconst second = (cells.push(2), 2);\nexport { first, second };\n";
    expect_contradicted(
        || {
            FixtureOpts::new(
                SOURCE,
                vec![logical_module(
                    "all",
                    &[
                        Member::new("cells"),
                        Member::new("append"),
                        Member::new("first"),
                        Member::new("second"),
                    ],
                )],
            )
            .with_unassigned_mode(unassigned_mode_inline())
            .with_commuting_statements(
                "static/app",
                vec![commuting_pair(
                    member_source_unit("all", "first"),
                    member_source_unit("all", "second"),
                )],
            )
        },
        &[ENTRY, CONTRADICTION, "array contents"],
    );
}

/// The direct nested invocation keeps its modeled cell through the parent
/// outcome, including a repeated call that can reuse an invocation memo.
#[test]
fn nested_callee_heap_contents_conflict_rejects_the_pair() {
    const SOURCE: &str = "const cells = [];\nfunction append() { cells.push(1); }\nfunction nested() { append(); }\nconst first = (nested(), nested(), 1);\nconst second = (cells.push(2), 2);\nexport { first, second };\n";
    expect_contradicted(
        || {
            FixtureOpts::new(
                SOURCE,
                vec![logical_module(
                    "all",
                    &[
                        Member::new("cells"),
                        Member::new("append"),
                        Member::new("nested"),
                        Member::new("first"),
                        Member::new("second"),
                    ],
                )],
            )
            .with_unassigned_mode(unassigned_mode_inline())
            .with_commuting_statements(
                "static/app",
                vec![commuting_pair(
                    member_source_unit("all", "first"),
                    member_source_unit("all", "second"),
                )],
            )
        },
        &[ENTRY, CONTRADICTION, "array contents"],
    );
}

/// An opaque host call's possible callback is an assumed path. Its body cell
/// still orders conservatively, but the assertion may discharge that order.
#[test]
fn assumed_callee_heap_contents_remain_inertable() {
    const SOURCE: &str = "const cells = [];\nfunction append() { cells.push(1); }\nconst serialize = JSON.stringify;\nconst first = (serialize([append]), 1);\nconst second = (cells.push(2), 2);\nexport { first, second };\n";
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "all",
                &[
                    Member::new("cells"),
                    Member::new("append"),
                    Member::new("serialize"),
                    Member::new("first"),
                    Member::new("second"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements(
            "static/app",
            vec![commuting_pair(
                member_source_unit("all", "first"),
                member_source_unit("all", "second"),
            )],
        ),
    );
    let validate = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    let report: serde_json::Value = serde_json::from_str(&validate.stdout).unwrap();
    assert_eq!(report["validation"]["success"], true, "{report:#}");
}

/// The same body can be reached both through an assumed host callback and a
/// direct call. Either source order must retain the direct cell as non-assumed.
#[test]
fn direct_callee_heap_contents_win_over_assumed_path_in_both_orders() {
    const ASSUMED_FIRST: &str = "const cells = [];\nfunction append() { cells.push(1); }\nconst serialize = JSON.stringify;\nconst first = (serialize([append]), append(), 1);\nconst second = (cells.push(2), 2);\nexport { first, second };\n";
    const DIRECT_FIRST: &str = "const cells = [];\nfunction append() { cells.push(1); }\nconst serialize = JSON.stringify;\nconst first = (append(), serialize([append]), 1);\nconst second = (cells.push(2), 2);\nexport { first, second };\n";
    for source in [ASSUMED_FIRST, DIRECT_FIRST] {
        expect_contradicted(
            || {
                FixtureOpts::new(
                    source,
                    vec![logical_module(
                        "all",
                        &[
                            Member::new("cells"),
                            Member::new("append"),
                            Member::new("serialize"),
                            Member::new("first"),
                            Member::new("second"),
                        ],
                    )],
                )
                .with_unassigned_mode(unassigned_mode_inline())
                .with_commuting_statements(
                    "static/app",
                    vec![commuting_pair(
                        member_source_unit("all", "first"),
                        member_source_unit("all", "second"),
                    )],
                )
            },
            &[ENTRY, CONTRADICTION, "array contents"],
        );
    }
}

/// A binding's write/read timeline is a derived conflict in either S-chain
/// mode.
#[test]
fn known_binding_write_after_read_rejects_the_pair() {
    const SOURCE: &str = "let count = 0;\nconst snap = count;\ncount = 1;\nconsole.log(snap, count);\nexport { count, snap };\n";
    expect_contradicted(
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
    expect_contradicted(
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
    expect_contradicted(
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
/// joins them: the premise buys nothing. All three live in one module, so the
/// run passes without any entry.
#[test]
fn pair_without_a_sequenced_edge_is_an_unused_warning() {
    let one_module = |pairs| {
        FixtureOpts::new(
            OPAQUE_SOURCE,
            vec![logical_module_with_anon(
                "probes/outer",
                &[
                    Member::new("trace"),
                    Member::new("alpha"),
                    Member::new("beta"),
                    Member::new("gamma"),
                ],
                &[RECORD_INSTALL],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements("static/app", pairs)
    };
    expect_unused_is_a_warning(
        one_module,
        commuting_pair(
            member_source_unit("probes/outer", "alpha"),
            member_source_unit("probes/outer", "gamma"),
        ),
        OPAQUE_OUTPUT,
        (
            ENTRY,
            UnusedPremiseReason::NoSequencedEdge,
            [
                ExpectedEndpoint {
                    module: "probes/outer",
                    name: "alpha",
                },
                ExpectedEndpoint {
                    module: "probes/outer",
                    name: "gamma",
                },
            ],
        ),
    );
}

/// `callback` plus 64 inert callbacks: a host call receiving the array
/// exceeds the interpreter's callback pool, which then assumes every pooled
/// callback may run (`callback_pool_limit`).
fn padded_pool(callback: &str) -> String {
    let padding = vec!["() => 3"; 64].join(", ");
    format!("serialize([{callback}, {padding}])")
}

/// `first` and `second` touch `state` through `bump` (writes) and `peek`
/// (reads); `third` reads `second`. With `first`/`third` in `outer` and
/// `second` in `inner`, the `state` timeline `second -> first` closes an
/// `outer`/`inner` cycle.
fn state_source(first: &str, second: &str) -> String {
    format!(
        "let state = 0;\nfunction bump() {{ state = 1; return 3; }}\nfunction peek() {{ return state; }}\nconst serialize = JSON.stringify;\nconst first = {first};\nconst second = {second};\nconst third = String(second).length;\nconsole.log(String(first).length, third, state);\nexport {{ state, bump, peek, first, second, third }};\n"
    )
}

const STATE_MODULES: [&str; 2] = ["probes/outer", "probes/inner"];

fn state_split(source: &str, pairs: Vec<CommutingStatementsContract>) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "cells/state",
                &[
                    Member::new("state"),
                    Member::new("bump"),
                    Member::new("peek"),
                    Member::new("serialize"),
                ],
            ),
            logical_module(
                "probes/outer",
                &[Member::new("first"), Member::new("third")],
            ),
            logical_module("probes/inner", &[Member::new("second")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_commuting_statements("static/app", pairs)
}

fn first_second_pair() -> CommutingStatementsContract {
    commuting_pair(
        member_source_unit("probes/outer", "first"),
        member_source_unit("probes/inner", "second"),
    )
}

/// The `binding_timeline` causes of `state` the premise inerted, as wire JSON.
fn inerted_state_timelines(fixture: &Fixture) -> Vec<serde_json::Value> {
    let graph = fixture.dependency_graph();
    inerted_edges(&graph, &premise(0))
        .into_iter()
        .filter_map(|edge| edge.sequenced_cause.as_ref())
        .map(|cause| serde_json::to_value(cause).expect("cause serializes"))
        .filter(|cause| cause["rule"] == "binding_timeline" && cause["binding"] == "state")
        .collect()
}

fn matches_expected_reason(actual: &serde_json::Value, expected: &serde_json::Value) -> bool {
    if expected["path"] != "possible_getter" {
        return actual == expected;
    }
    actual["path"] == "possible_getter"
        && actual["lookup"]["lexical_owner"].is_number()
        && actual["lookup"]["span_start"].is_number()
        && actual["lookup"]["span_end"].is_number()
        && matches!(
            actual["promotion"]["kind"].as_str(),
            Some("lazy_owner" | "raw_callbacks")
        )
}

/// Both endpoints are incomplete: each hands a pool past the bound to
/// `JSON.stringify`, so the `state` write and read exist only because the
/// fallback assumes every pooled callback may run. That is opacity, not a
/// derived conflict, so the pair is accepted (and holds: `JSON.stringify`
/// never calls a function element).
#[test]
fn timeline_between_two_fallback_effects_is_inertable() {
    let source = state_source(&padded_pool("bump"), &padded_pool("peek"));
    // Positive anchor: without the pair the `state` timeline closes the cycle.
    let without = expect_cycle_rejection(state_split(&source, vec![]), &STATE_MODULES);
    assert!(
        without["cut"].as_array().expect("cut").iter().any(|edge| {
            let cause = &edge["explanation"]["cause"];
            cause["rule"] == "binding_timeline" && cause["binding"] == "state"
        }),
        "{without:#}"
    );

    let fixture = run_fixture(state_split(&source, vec![first_second_pair()]));
    assert_preserved_entry_output(&fixture, "326 326 0\n");
    let timelines = inerted_state_timelines(&fixture);
    assert!(!timelines.is_empty(), "no `state` timeline inerted");
    for cause in &timelines {
        for side in ["source", "target"] {
            assert_eq!(cause[side]["kind"], "fallback", "{cause:#}");
            let reason = &cause[side]["reason"];
            assert_eq!(reason["path"], "fallback_summary", "{cause:#}");
            assert_eq!(reason["loss"]["code"], "callback_pool_limit", "{cause:#}");
            assert_eq!(reason["loss"]["limit"], 64, "{cause:#}");
        }
    }
}

/// Soundness: the same shape with both effects observed (direct calls the
/// interpreter executes completely) is a derived conflict; the pair is still
/// rejected.
#[test]
fn timeline_between_two_observed_effects_rejects_the_pair() {
    let source = state_source("bump()", "peek()");
    expect_contradicted(
        || state_split(&source, vec![first_second_pair()]),
        &[
            ENTRY,
            CONTRADICTION,
            "`first`",
            "`second`",
            "timeline of `state`",
        ],
    );
}

/// An observed write against a fallback read is inertable. Only the read is
/// assumed, and the author asserts the pooled `peek` does not run during
/// `second`'s initialization (`JSON.stringify` never calls it), so `second`
/// cannot observe `first`'s write. Had `peek` run, the assertion would be
/// false; the engine never derived that it runs.
#[test]
fn timeline_between_observed_write_and_fallback_read_is_inertable() {
    let source = state_source("bump()", &padded_pool("peek"));
    expect_cycle_rejection(state_split(&source, vec![]), &STATE_MODULES);

    let fixture = run_fixture(state_split(&source, vec![first_second_pair()]));
    assert_preserved_entry_output(&fixture, "1 326 1\n");
    let timelines = inerted_state_timelines(&fixture);
    assert!(!timelines.is_empty(), "no `state` timeline inerted");
    for cause in &timelines {
        assert_eq!(cause["source"]["kind"], "fallback", "{cause:#}");
        assert_eq!(
            cause["target"],
            serde_json::json!({"kind": "observed"}),
            "{cause:#}"
        );
    }
}

/// `prelude` declares helpers (`helpers`) beside `state`; `first`, `second`
/// and `third` close the `outer`/`inner` cycle as in [`state_source`].
fn probe_source(prelude: &str, helpers: &[&str], first: &str, second: &str) -> String {
    let exports = helpers.join(", ");
    format!(
        "let state = 0;\nfunction bump() {{ state = 1; return 3; }}\nfunction peek() {{ return state; }}\n{prelude}\nconst first = {first};\nconst second = {second};\nconst third = typeof second;\nconsole.log(typeof first, third, state);\nexport {{ state, bump, peek, {exports}, first, second, third }};\n"
    )
}

fn probe_split<'a>(
    source: &'a str,
    helpers: &[&'static str],
    pairs: Vec<CommutingStatementsContract>,
) -> FixtureOpts<'a> {
    let cells: Vec<Member> = ["state", "bump", "peek"]
        .iter()
        .chain(helpers)
        .copied()
        .map(Member::new)
        .collect();
    FixtureOpts::new(
        source,
        vec![
            logical_module("cells/state", &cells),
            logical_module(
                "probes/outer",
                &[Member::new("first"), Member::new("third")],
            ),
            logical_module("probes/inner", &[Member::new("second")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_commuting_statements("static/app", pairs)
}

/// The `state` conflict between `first` and `second` exists only through
/// conservative paths, `path` among them: without the pair its timeline
/// closes the cycle; with it the pair is accepted, every inerted `state`
/// timeline is assumed on both sides, `path` assumed at least one effect, and
/// the bundle prints `output`.
fn assert_assumed_pair_accepted(
    source: &str,
    helpers: &[&'static str],
    path: serde_json::Value,
    output: &str,
) {
    let without = expect_cycle_rejection(probe_split(source, helpers, vec![]), &STATE_MODULES);
    assert!(
        without["cut"].as_array().expect("cut").iter().any(|edge| {
            let cause = &edge["explanation"]["cause"];
            cause["rule"] == "binding_timeline" && cause["binding"] == "state"
        }),
        "{without:#}"
    );
    let fixture = run_fixture(probe_split(source, helpers, vec![first_second_pair()]));
    assert_preserved_entry_output(&fixture, output);
    let timelines = inerted_state_timelines(&fixture);
    assert!(!timelines.is_empty(), "no `state` timeline inerted");
    for cause in &timelines {
        for side in ["source", "target"] {
            assert_eq!(cause[side]["kind"], "fallback", "{cause:#}");
        }
    }
    assert!(
        timelines.iter().any(|cause| {
            ["source", "target"]
                .iter()
                .any(|side| matches_expected_reason(&cause[side]["reason"], &path))
        }),
        "{path} assumed no inerted effect: {timelines:#?}"
    );
}

/// The same shape with `first` writing and `second` reading `state` through
/// direct calls the statement definitely makes: a derived conflict.
fn assert_observed_pair_rejected(source: &str, helpers: &[&'static str]) {
    expect_contradicted(
        || probe_split(source, helpers, vec![first_second_pair()]),
        &[
            ENTRY,
            CONTRADICTION,
            "`first`",
            "`second`",
            "timeline of `state`",
        ],
    );
}

const GLOBAL_OBJECT_CALLBACK_SOURCE: &str = r#"let state = 0;
function bump() { state = 1; return 3; }
function peek() { return state; }
const serialize = JSON.stringify;
const escaped = serialize([bump, peek]);
const first = typeof window < "u" && window.addEventListener("probe", () => {});
const second = bump();
const third = String(second).length;
console.log(String(first).length, third, state);
export { state, bump, peek, serialize, escaped, first, second, third };
"#;

const KNOWN_CALLBACK_CONTROL_SOURCE: &str = r#"let state = 0;
function peek() { return state; }
const directRead = peek();
const getterSource = { get value() { return state; } };
const getterRead = getterSource.value;
const api = { read: () => state };
const methodRead = api.read();
const mixed = globalThis.__probe_flag ? globalThis.unknown : peek;
const mixedRead = mixed();
state = 1;
console.log(directRead, getterRead, methodRead, mixedRead, state);
export { state, directRead, getterRead, methodRead, mixedRead };
"#;

fn global_object_callback_split(pairs: Vec<CommutingStatementsContract>) -> FixtureOpts<'static> {
    FixtureOpts::new(
        GLOBAL_OBJECT_CALLBACK_SOURCE,
        vec![
            logical_module(
                "cells/state",
                &[
                    Member::new("state"),
                    Member::new("bump"),
                    Member::new("peek"),
                    Member::new("serialize"),
                    Member::new("escaped"),
                ],
            ),
            logical_module(
                "probes/outer",
                &[Member::new("first"), Member::new("third")],
            ),
            logical_module("probes/inner", &[Member::new("second")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_commuting_statements("static/app", pairs)
}

fn propose_global_object_pair(rejected: &RejectedFixture) -> serde_json::Value {
    let graph = rejected.report_path("dependency_graph.json.gz");
    let output = run_debundle(&[
        "spec",
        "propose-commutations",
        "--graph",
        graph.to_str().unwrap(),
        "--source-root",
        rejected.source_root.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        output.status.success(),
        "propose-commutations failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("commutation proposal JSON")
}

fn effect_provenance_for_binding(
    graph: &DependencyGraphReport,
    source_unit: &str,
    binding: &str,
) -> Vec<serde_json::Value> {
    graph
        .edges
        .iter()
        .filter_map(|edge| {
            let cause = edge.sequenced_cause.as_ref()?;
            let cause = serde_json::to_value(cause).expect("cause serializes");
            if cause["rule"] != "binding_timeline" || cause["binding"] != binding {
                return None;
            }
            if edge.source == source_unit {
                Some(cause["source"].clone())
            } else if edge.target == source_unit {
                Some(cause["target"].clone())
            } else {
                None
            }
        })
        .collect()
}

#[test]
fn global_object_callback_candidates_are_assumed_and_known_calls_stay_observed() {
    let rejected = run_rejection_fixture(global_object_callback_split(vec![]));
    let proposal = propose_global_object_pair(&rejected);
    let pair_is_proposed = proposal["proposals"].as_array().is_some_and(|proposals| {
        proposals.iter().any(|proposal| {
            let mut bindings: Vec<_> = ["a", "b"]
                .iter()
                .filter_map(|end| proposal[*end]["binding"].as_str())
                .collect();
            bindings.sort_unstable();
            bindings == ["first", "second"]
        })
    });
    assert!(
        pair_is_proposed,
        "the possible global callback conflict should be inertable; proposal report: {proposal:#}"
    );

    let fixture = run_fixture(global_object_callback_split(vec![first_second_pair()]));
    assert_preserved_entry_output(&fixture, "5 1 1\n");
    let graph = fixture.dependency_graph();
    let timelines = inerted_state_timelines(&fixture);
    assert!(
        !timelines.is_empty(),
        "the pair inerted no `state` timeline"
    );
    for cause in &timelines {
        assert_eq!(cause["source"]["kind"], "observed", "{cause:#}");
        assert_eq!(cause["target"]["kind"], "fallback", "{cause:#}");
    }

    let first = source_unit_for_binding(&graph, "first");
    let first_effects = effect_provenance_for_binding(&graph, first, "state");
    assert!(
        first_effects.iter().any(|effect| {
            effect["kind"] == "fallback" && effect["reason"]["path"] == "opaque_callback_invocation"
        }),
        "global lookup callbacks must keep their possible-call provenance: {first_effects:#?}"
    );
    let second = source_unit_for_binding(&graph, "second");
    let second_effects = effect_provenance_for_binding(&graph, second, "state");
    assert!(
        !second_effects.is_empty()
            && second_effects
                .iter()
                .all(|effect| effect["kind"] == "observed"),
        "the direct `bump()` effect must remain observed: {second_effects:#?}"
    );
}

#[test]
fn known_callback_control_provenance_baseline() {
    let controls = run_fixture(FixtureOpts::new(KNOWN_CALLBACK_CONTROL_SOURCE, vec![]));
    assert_preserved_entry_output(&controls, "0 0 0 0 1\n");
    let graph = controls.dependency_graph();
    for binding in ["directRead", "getterRead", "methodRead", "mixedRead"] {
        let source_unit = source_unit_for_binding(&graph, binding);
        let provenance = effect_provenance_for_binding(&graph, source_unit, "state");
        assert!(
            !provenance.is_empty(),
            "{binding} has no `state` timeline effect"
        );
        let matches_baseline = if binding == "getterRead" {
            provenance.iter().all(|effect| {
                effect["kind"] == "fallback"
                    && effect["reason"]["path"] == "escaped_callback_closure"
            })
        } else {
            provenance.iter().all(|effect| effect["kind"] == "observed")
        };
        assert!(
            matches_baseline,
            "{binding} provenance changed from the established baseline: {provenance:#?}"
        );
    }
}

/// `bump`/`peek` escaped to `JSON.stringify`.
const ESCAPE_PRELUDE: &str = "const escape = JSON.stringify([bump, peek]);";

/// [`ESCAPE_PRELUDE`] after writes to the exact `Object` slots read below, so the native
/// census no longer proves `Object`'s own data slots intact: reading one may
/// yield or run any escaped callback (a replaced getter could), and the
/// interpreter promotes their closures. Neither alias read calls them. Each
/// probe ends in a host call so its strict S-chain edge is inertable; the
/// read runs first and contributes `state` first.
const NATIVE_ESCAPE_PRELUDE: &str = "const mark = (Object.defineProperty = Object.defineProperty, Object.getOwnPropertyDescriptor = Object.getOwnPropertyDescriptor);\nconst escape = JSON.stringify([bump, peek]);";

#[test]
fn native_member_read_promoting_escaped_callbacks_is_inertable() {
    let source = probe_source(
        NATIVE_ESCAPE_PRELUDE,
        &["mark", "escape"],
        "(Object.defineProperty, JSON.stringify(1))",
        "(Object.getOwnPropertyDescriptor, JSON.stringify(2))",
    );
    assert_assumed_pair_accepted(
        &source,
        &["mark", "escape"],
        serde_json::json!({"path": "escaped_callback_closure"}),
        "string string 0\n",
    );
}

#[test]
fn native_member_read_beside_observed_effects_rejects_the_pair() {
    let source = probe_source(
        NATIVE_ESCAPE_PRELUDE,
        &["mark", "escape"],
        "(bump(), Object.defineProperty, JSON.stringify(1))",
        "(peek(), Object.getOwnPropertyDescriptor, JSON.stringify(2))",
    );
    assert_observed_pair_rejected(&source, &["mark", "escape"]);
}

/// A host call within the callback pool bound invokes each callback it
/// received as if it ran; `JSON.stringify` never calls a function element.
#[test]
fn opaque_call_invoking_its_callback_arguments_is_inertable() {
    let source = probe_source(
        &format!("{ESCAPE_PRELUDE}\nconst serialize = JSON.stringify;"),
        &["escape", "serialize"],
        "serialize([bump])",
        "serialize([peek])",
    );
    assert_assumed_pair_accepted(
        &source,
        &["escape", "serialize"],
        serde_json::json!({"path": "opaque_callback_invocation"}),
        "string string 0\n",
    );
}

/// `box` escaped to `JSON.stringify`, so its fields may since hold anything
/// (an accessor among them): reading one promotes the closure of the
/// callbacks it may yield, and the getter itself may run at the read. The trailing host
/// call keeps each statement opaque, so their strict S-chain edge is
/// inertable too; it runs after the read, which contributed `state` first.
const BOX_PRELUDE: &str = "const box = { f: bump, g: peek };\nconst boxed = JSON.stringify(box);";

#[test]
fn escaped_record_member_read_closure_is_inertable() {
    let source = probe_source(
        BOX_PRELUDE,
        &["box", "boxed"],
        "(box.f, JSON.stringify(1))",
        "(box.g, JSON.stringify(2))",
    );
    assert_assumed_pair_accepted(
        &source,
        &["box", "boxed"],
        serde_json::json!({"path": "possible_getter"}),
        "string string 0\n",
    );
}

#[test]
fn escaped_record_member_read_beside_observed_effects_rejects_the_pair() {
    let source = probe_source(
        BOX_PRELUDE,
        &["box", "boxed"],
        "(bump(), box.f, JSON.stringify(1))",
        "(peek(), box.g, JSON.stringify(2))",
    );
    assert_observed_pair_rejected(&source, &["box", "boxed"]);
}

/// `arguments` defeats input modeling, so a call to `touch`/`look` promotes
/// their whole lazy bodies; the `later` closures that touch `state` never
/// run.
#[test]
fn whole_body_promotion_for_unsupported_inputs_is_inertable() {
    let prelude = "function touch({}) { const later = () => { state = 2; }; return typeof later.length; }\nfunction look({}) { const later = () => state; return typeof later.length; }";
    let source = probe_source(prelude, &["touch", "look"], "touch({})", "look({})");
    assert_assumed_pair_accepted(
        &source,
        &["touch", "look"],
        serde_json::json!({"path": "whole_body_promotion", "operation": "callable_inputs"}),
        "string string 0\n",
    );
}

#[test]
fn unsupported_inputs_beside_observed_effects_rejects_the_pair() {
    let prelude = "function touch() { state = 2; return arguments.length; }\nfunction look() { return state + arguments.length; }";
    let source = probe_source(prelude, &["touch", "look"], "touch()", "look()");
    assert_observed_pair_rejected(&source, &["touch", "look"]);
}
