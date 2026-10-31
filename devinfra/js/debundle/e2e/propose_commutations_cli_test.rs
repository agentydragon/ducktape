//! `debundle spec propose-commutations` over real rejection reports: the
//! command drafts `commuting_statements` entries for the `sequenced` cut edges
//! the gate would accept, and reports — without emitting — the cut edges a
//! premise can never inert, the pairs the gate would reject, the endpoints
//! without a unique selector, and every SCC that pairs alone cannot break.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use debundle_e2e_support::*;
use serde::Deserialize;
use serde_json::Value;

/// `globalThis.record` is a callable the analysis cannot see into, so every
/// call to it is an opaque effect ordered only conservatively.
const OPAQUE_SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nconst alpha = (globalThis.record(\"alpha\"), \"A\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nconst gamma = (globalThis.record(\"gamma\"), \"C\");\nconsole.log(alpha, beta, gamma, trace.slice().sort().join(\",\"));\nexport { alpha, beta, gamma };\n";
const RECORD_INSTALL: &str = "globalThis.record = (key) => { trace.push(key); };";
const OPAQUE_OUTPUT: &str = "A B C alpha,beta,gamma\n";

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

fn propose(graph: &Path, source_root: &Path, extra: &[&str]) -> Value {
    let mut args = vec![
        "spec",
        "propose-commutations",
        "--graph",
        graph.to_str().unwrap(),
        "--source-root",
        source_root.to_str().unwrap(),
        "--format",
        "json",
    ];
    args.extend_from_slice(extra);
    let out = run_debundle(&args);
    assert!(
        out.status.success(),
        "propose-commutations failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    parse_stdout_json(&out)
}

fn propose_rejected(rejected: &RejectedFixture, extra: &[&str]) -> Value {
    propose(
        &rejected.report_path("dependency_graph.json"),
        &rejected.source_root,
        extra,
    )
}

fn array<'a>(report: &'a Value, key: &str) -> &'a [Value] {
    report[key]
        .as_array()
        .unwrap_or_else(|| panic!("{key} must be an array: {report:#}"))
}

fn only_scc(report: &Value) -> &Value {
    let [scc] = array(report, "sccs") else {
        panic!("one SCC: {report:#}");
    };
    assert_eq!(scc["scc_id"], 0);
    scc
}

/// `(kind, reason)` of every blocking-edge row of an SCC pairs cannot break.
fn blocking_rows(scc: &Value) -> Vec<(String, String)> {
    assert_eq!(scc["verdict"], "pairs_cannot_break", "{scc:#}");
    array(scc, "blocking_edges")
        .iter()
        .map(|row| {
            assert!(
                row["count"].as_u64().is_some_and(|count| count > 0),
                "{row:#}"
            );
            (
                row["kind"].as_str().expect("kind").to_owned(),
                row["reason"].as_str().expect("reason").to_owned(),
            )
        })
        .collect()
}

/// The draft is the `chunk_analysis_options` fragment the author pastes.
#[derive(Deserialize)]
struct Draft {
    chunk_analysis_options: BTreeMap<String, spec::DependencyGraphOptions>,
}

fn draft_pairs(report: &Value) -> Vec<CommutingStatementsContract> {
    let yaml = report["draft_yaml"].as_str().expect("draft_yaml");
    let draft: Draft =
        serde_yaml::from_str(yaml).unwrap_or_else(|err| panic!("draft parses: {err}\n{yaml}"));
    let mut chunks = draft.chunk_analysis_options.into_iter();
    let (chunk, options) = chunks.next().expect("one chunk in the draft");
    assert_eq!(chunk, "static/app");
    assert!(chunks.next().is_none(), "one chunk in the draft");
    options.commuting_statements
}

#[test]
fn sequenced_only_cycle_proposes_the_pair_the_gate_accepts() {
    let rejected = run_rejection_fixture(opaque_split(vec![]));
    let draft_path = rejected.report_root.join("commutations.yaml");
    let report = propose_rejected(&rejected, &["--out", draft_path.to_str().unwrap()]);

    // The at-init `trace` read through `globalThis.record` also orders
    // `beta` after `trace`'s initializer: a derived conflict, reported with
    // the gate's cause and never drafted.
    for candidate in array(&report, "rejected") {
        assert_eq!(candidate["reason"], "known_conflict", "{candidate:#}");
        assert_eq!(
            candidate["conflicts"][0]["conflict"]["rule"], "binding_timeline",
            "{candidate:#}"
        );
    }
    // Both sequenced arcs of the two-module cycle are candidates. Only
    // `gamma -> beta` is alone on its arc; `beta -> alpha` shares the other
    // arc with the at-init reads of `trace` through `globalThis.record`, so
    // inerting it cannot break the SCC.
    let [first, second] = array(&report, "proposals") else {
        panic!("two proposals: {report:#}");
    };
    assert_eq!(first["rank"], 1);
    assert_eq!(first["rank_reason"], "single_pair_cut");
    assert_eq!(first["single_pair_cut"], true);
    assert_eq!(first["remaining_module_pair_edges"], 0);
    assert_eq!(second["rank"], 2);
    assert_eq!(second["rank_reason"], "partial_cut");
    assert_eq!(second["single_pair_cut"], false);
    let bindings = |proposal: &Value| -> Vec<String> {
        let mut bindings: Vec<String> = ["a", "b"]
            .iter()
            .map(|end| {
                proposal[end]["binding"]
                    .as_str()
                    .expect("binding")
                    .to_owned()
            })
            .collect();
        bindings.sort();
        bindings
    };
    assert_eq!(bindings(first), ["beta", "gamma"]);
    assert_eq!(bindings(second), ["alpha", "beta"]);
    for proposal in [first, second] {
        assert_eq!(proposal["scc_id"], 0);
        let modules: Vec<&str> = ["a", "b"]
            .iter()
            .map(|end| proposal[end]["module"].as_str().expect("module"))
            .collect();
        assert!(
            modules.contains(&"probes/outer") && modules.contains(&"probes/inner"),
            "{proposal:#}"
        );
        for end in ["a", "b"] {
            let endpoint = &proposal[end];
            // Without `--modules` every endpoint is a synthesized, proven
            // `source_match` claiming the declared binding.
            assert_eq!(endpoint["selector_origin"], "synthesized_source_match");
            assert!(
                endpoint["source_location"]["start_line"].is_number(),
                "{endpoint:#}"
            );
            assert!(endpoint["excerpt"].is_string(), "{endpoint:#}");
            let bindings = proposal["entry"][end]["source_match"]["bindings"]
                .as_array()
                .expect("one claimed binding");
            assert_eq!(bindings.len(), 1, "{proposal:#}");
        }
        let [inerted] = array(proposal, "inerted_edges") else {
            panic!("one inerted edge: {proposal:#}");
        };
        assert_eq!(inerted["explanation"]["rule"], "strict_chain");
        assert_eq!(proposal["entry"]["note"], Value::Null);
    }

    let draft = fs::read_to_string(&draft_path).expect("draft written");
    assert_eq!(report["draft_written"], draft_path.to_str().unwrap());
    assert_eq!(draft, report["draft_yaml"].as_str().unwrap());
    let drafted = draft_pairs(&report);
    assert_eq!(
        drafted.len(),
        2,
        "the draft lists every proposal in rank order"
    );
    let first_entry: CommutingStatementsContract =
        serde_json::from_value(first["entry"].clone()).expect("entry is a contract");
    assert_eq!(drafted[0], first_entry);

    // Positive anchor: the single-pair cut alone clears the cycle under the
    // gate and the emitted bundle preserves the entry output.
    let fixture = run_fixture(opaque_split(vec![first_entry]));
    assert_preserved_entry_output(&fixture, OPAQUE_OUTPUT);

    let scc = only_scc(&report);
    assert_eq!(scc["verdict"], "pairs_break", "{scc:#}");
    assert_eq!(array(scc, "modules").len(), 2, "{scc:#}");
}

/// The source unit of a comma-list declarator is the declarator, not the
/// declaration: its excerpt starts at its own binding.
#[test]
fn comma_list_declarator_excerpt_is_the_declarator() {
    const SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nconst alpha = (globalThis.record(\"alpha\"), \"A\"),\n  beta = (globalThis.record(\"beta\"), \"B\"),\n  gamma = (globalThis.record(\"gamma\"), \"C\");\nconsole.log(alpha, beta, gamma, trace.slice().sort().join(\",\"));\nexport { alpha, beta, gamma };\n";
    let rejected = run_rejection_fixture(
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
                logical_module("probes/inner", &[Member::new("beta")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let report = propose_rejected(&rejected, &[]);
    let proposals = array(&report, "proposals");
    assert!(!proposals.is_empty(), "{report:#}");
    let mut excerpted = Vec::new();
    for endpoint in proposals.iter().flat_map(|p| [&p["a"], &p["b"]]) {
        let binding = endpoint["binding"].as_str().expect("binding");
        let value = match binding {
            "alpha" => "A",
            "beta" => "B",
            "gamma" => "C",
            other => panic!("unexpected endpoint {other}: {endpoint:#}"),
        };
        let expected = format!("{binding} = (globalThis.record(\"{binding}\"), \"{value}\")");
        assert_eq!(endpoint["excerpt"], expected.as_str(), "{endpoint:#}");
        excerpted.push(binding);
    }
    assert!(
        excerpted.iter().any(|binding| *binding != "alpha"),
        "a declarator after the first is excerpted: {report:#}"
    );
}

/// At-init reads are dataflow dependences: a premise never inerts them, so an
/// eager-only cycle yields no proposal and every cut edge needs co-location.
#[test]
fn eager_cycle_needs_co_location() {
    const SOURCE: &str = "const alpha = 1;\nconst beta = alpha + 1;\nconst gamma = beta + 1;\nconsole.log(alpha, beta, gamma);\nexport { alpha, beta, gamma };\n";
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module(
                    "probes/outer",
                    &[Member::new("alpha"), Member::new("gamma")],
                ),
                logical_module("probes/inner", &[Member::new("beta")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let report = propose_rejected(&rejected, &[]);
    assert!(array(&report, "proposals").is_empty(), "{report:#}");
    assert!(array(&report, "rejected").is_empty(), "{report:#}");
    let needs = array(&report, "needs_co_location");
    assert!(!needs.is_empty(), "{report:#}");
    for need in needs {
        assert_eq!(need["edge"]["kind"], "eager_use", "{need:#}");
        assert_eq!(need["scc_id"], 0);
    }
    // No pair exists, and the SCC row says so instead of staying silent.
    let scc = only_scc(&report);
    assert_eq!(scc["residual_modules"], 2, "{scc:#}");
    for row in blocking_rows(scc) {
        assert_eq!(
            row,
            ("eager_use".to_owned(), "kind_not_inertable".to_owned())
        );
    }
    assert_eq!(
        draft_pairs(&report),
        Vec::<CommutingStatementsContract>::new()
    );
}

/// A `sequenced` cut edge beside at-init edges in one SCC: the pair may be
/// proposed, but never as a single-pair cut, since the at-init reads keep
/// the SCC closed.
#[test]
fn mixed_eager_cycle_never_claims_a_single_pair_cut() {
    const SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nconst alpha = (globalThis.record(\"alpha\"), \"A\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nconst gamma = beta.length;\nconst delta = gamma + 1;\nconsole.log(alpha, beta, gamma, delta, trace.slice().sort().join(\",\"));\nexport { alpha, beta, gamma, delta };\n";
    let rejected = run_rejection_fixture(
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
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let report = propose_rejected(&rejected, &[]);
    assert!(
        !array(&report, "needs_co_location").is_empty(),
        "{report:#}"
    );
    for proposal in array(&report, "proposals") {
        assert_eq!(proposal["single_pair_cut"], false, "{proposal:#}");
        assert_eq!(proposal["rank_reason"], "partial_cut", "{proposal:#}");
    }
    let rows = blocking_rows(only_scc(&report));
    assert!(
        rows.contains(&("eager_use".to_owned(), "kind_not_inertable".to_owned())),
        "{rows:?}"
    );
}

/// A known write/read conflict on one `globalThis` property is the gate's
/// contradiction; the command reports it with the gate's own cause and emits
/// nothing.
#[test]
fn known_global_property_conflict_is_reported_not_proposed() {
    const SOURCE: &str = "globalThis.flag = \"first\";\nconst snapshot = globalThis.flag;\nglobalThis.flag = \"second\";\nconsole.log(snapshot, globalThis.flag);\nexport { snapshot };\n";
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "mod_w",
                    &[],
                    &[
                        "globalThis.flag = \"first\";",
                        "globalThis.flag = \"second\";",
                    ],
                ),
                logical_module("mod_read", &[Member::new("snapshot")]),
            ],
        )
        .with_dataflow_aware_s_chain()
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let report = propose_rejected(&rejected, &[]);
    assert!(array(&report, "proposals").is_empty(), "{report:#}");
    let rejected_candidates = array(&report, "rejected");
    assert!(!rejected_candidates.is_empty(), "{report:#}");
    let flag_cell = serde_json::json!({ "kind": "global_prop", "key": "flag" });
    for candidate in rejected_candidates {
        assert_eq!(candidate["reason"], "known_conflict", "{candidate:#}");
        let conflicts = candidate["conflicts"].as_array().expect("conflicts");
        assert!(!conflicts.is_empty(), "{candidate:#}");
        for conflict in conflicts {
            assert_eq!(conflict["conflict"]["kind"], "sequenced", "{conflict:#}");
            assert_eq!(conflict["conflict"]["rule"], "cell_order");
            assert!(
                conflict["conflict"]["conflicts"]
                    .as_array()
                    .expect("cell conflicts")
                    .iter()
                    .any(|relation| relation["cell"] == flag_cell),
                "{conflict:#}"
            );
        }
    }
    let rows = blocking_rows(only_scc(&report));
    assert!(
        rows.contains(&("sequenced".to_owned(), "known_conflict".to_owned())),
        "{rows:?}"
    );
}

/// An anonymous endpoint whose statement appears twice has no unique
/// selector; the pair is reported with the resolve's outcome and not drafted.
#[test]
fn duplicated_anonymous_endpoint_is_reported_without_a_selector() {
    // The callee touches no chunk binding, so the only edges between the
    // ticks and `beta` are opaque-call strict-chain adjacency, which a
    // premise could inert if the tick could be selected.
    const SOURCE: &str = "globalThis.record = (key) => { console.log(\"tick\", key); };\nglobalThis.record(\"tick\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nglobalThis.record(\"tick\");\nconsole.log(beta);\nexport { beta };\n";
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module("probes/inner", &[Member::new("beta")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let graph = rejected.dependency_graph();
    let beta = source_unit_for_binding(&graph, "beta");
    let report = propose_rejected(&rejected, &[]);
    assert!(array(&report, "proposals").is_empty(), "{report:#}");
    let rejected_candidates = array(&report, "rejected");
    assert!(!rejected_candidates.is_empty(), "{report:#}");
    for candidate in rejected_candidates {
        assert_eq!(candidate["reason"], "no_unique_selector", "{candidate:#}");
        assert_ne!(candidate["endpoint"], beta, "{candidate:#}");
        assert!(candidate["detail"].is_string(), "{candidate:#}");
        let pair = [candidate["a"].as_str(), candidate["b"].as_str()];
        assert!(pair.contains(&Some(beta)), "{candidate:#}");
    }
}

/// With the chunk's module tree, a logical member endpoint is written as
/// `resolved_member` under its authored module spelling.
#[test]
fn tree_spec_members_are_proposed_as_resolved_members() {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[("app", OPAQUE_SOURCE)],
            module_roots: &[("tree", "app")],
            modules: &[
                (
                    "tree/Probes/Outer.yaml",
                    "members:\n  - name: trace\n    selector:\n      binding: { name: trace }\n  - name: alpha\n    selector:\n      binding: { name: alpha }\n  - name: gamma\n    selector:\n      binding: { name: gamma }\nanonymous_statements:\n  - match: \"globalThis.record = (key) => { trace.push(key); };\"\n",
                ),
                (
                    "tree/Probes/Inner.yaml",
                    "members:\n  - name: beta\n    selector:\n      binding: { name: beta }\n",
                ),
            ],
        },
        &[],
    );
    assert_eq!(
        run.result.status.code(),
        Some(1),
        "the split must be rejected\nstdout:\n{}\nstderr:\n{}",
        run.result.stdout,
        run.result.stderr
    );
    let modules = run.root().join("modules/tree");
    let report = propose(
        &run.report_root.join("app/dependency_graph.json"),
        &run.root().join("snapshot"),
        &["--modules", modules.to_str().unwrap()],
    );
    let proposals = array(&report, "proposals");
    assert!(!proposals.is_empty(), "{report:#}");
    for (proposal, end) in proposals.iter().flat_map(|p| [(p, "a"), (p, "b")]) {
        assert_eq!(
            proposal[end]["selector_origin"], "resolved_member",
            "{proposal:#}"
        );
        let member = &proposal["entry"][end]["resolved_member"];
        assert!(
            ["Probes/Outer", "Probes/Inner"].contains(&member["module"].as_str().expect("module")),
            "authored spelling, not the canonical path: {proposal:#}"
        );
        assert!(
            ["alpha", "beta", "gamma"].contains(&member["name"].as_str().expect("name")),
            "{proposal:#}"
        );
    }
}

#[test]
fn print_invocation_classifies_read_only_analysis_with_a_draft_write() {
    let out = run_debundle(&[
        "spec",
        "propose-commutations",
        "--graph",
        "reports/tree/static/app/dependency_graph.json",
        "--source-root",
        "snapshot",
        "--out",
        "draft.yaml",
        "--print-invocation",
    ]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let record = parse_stdout_json(&out);
    assert_eq!(record["mode"], "read_only_analysis");
    assert_eq!(record["requested_proof"], "none");
    let scopes = record["write_scope"].as_array().expect("write_scope");
    assert!(
        scopes.iter().any(|scope| scope["scope"] == "draft"
            && scope["path"]
                .as_str()
                .is_some_and(|p| p.ends_with("draft.yaml"))),
        "{record:#}"
    );
}
