//! What an accepted `commuting_statements` pair buys the realizability gate,
//! and the source facts that reject one (SPEC.md § Commuting statements):
//!
//! - a `sequenced`-only cycle between two modules is cleared by asserting the
//!   pair, and the emitted bundle preserves the original entry output;
//! - `eager_use` edges are never inerted, so an eager cycle still blocks;
//! - a pair the analysis derived to conflict (a known write and read of one
//!   cell or binding, a shared `var`) is rejected even though asserted;
//! - a binding timeline is a derived conflict only between two observed
//!   effects; one a synchronous fallback merely assumed is inertable;
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

/// `spec validate` reports the rejected premise as one typed
/// `commuting_statements_diagnostics` entry of `kind` naming the entry and
/// fails validation; `run` rejects the spec with a diagnostic carrying every
/// token.
fn expect_rejected<'a>(build: impl Fn() -> FixtureOpts<'a>, kind: &str, tokens: &[&str]) {
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
    assert_eq!(diagnostic["kind"], kind, "{report:#}");
    assert!(
        diagnostic["spec_location"]
            .as_str()
            .is_some_and(|location| location.contains(ENTRY)),
        "{report:#}"
    );
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
        "unused",
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
        .join("static/app/dependency_graph.json");
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
        "contradicted",
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
        "contradicted",
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
        "contradicted",
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
        "contradicted",
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
        "unused",
        &[ENTRY, "inerts no sequenced edge", "`alpha`", "`gamma`"],
    );
}

/// `callback` plus 64 inert callbacks: a host call receiving the array
/// exceeds the interpreter's callback pool, which then assumes every pooled
/// callback may run (`callback_pool_limit`).
fn padded_pool(callback: &str) -> String {
    let padding = vec!["() => 3"; 64].join(", ");
    format!("JSON.stringify([{callback}, {padding}])")
}

/// `first` and `second` touch `state` through `bump` (writes) and `peek`
/// (reads); `third` reads `second`. With `first`/`third` in `outer` and
/// `second` in `inner`, the `state` timeline `second -> first` closes an
/// `outer`/`inner` cycle.
fn state_source(first: &str, second: &str) -> String {
    format!(
        "let state = 0;\nfunction bump() {{ state = 1; return 3; }}\nfunction peek() {{ return state; }}\nconst first = {first};\nconst second = {second};\nconst third = String(second).length;\nconsole.log(String(first).length, third, state);\nexport {{ state, bump, peek, first, second, third }};\n"
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
            assert_eq!(
                cause[side],
                serde_json::json!({
                    "kind": "fallback",
                    "reason": {"code": "callback_pool_limit", "limit": 64},
                }),
                "{cause:#}"
            );
        }
    }
}

/// Soundness: the same shape with both effects observed (direct calls the
/// interpreter executes completely) is a derived conflict; the pair is still
/// rejected.
#[test]
fn timeline_between_two_observed_effects_rejects_the_pair() {
    let source = state_source("bump()", "peek()");
    expect_rejected(
        || state_split(&source, vec![first_second_pair()]),
        "contradicted",
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
