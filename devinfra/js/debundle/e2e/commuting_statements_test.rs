//! `chunk_analysis_options.<chunk>.commuting_statements` resolves each endpoint
//! to exactly one source unit of its chunk and rejects malformed pairs. What
//! an accepted pair buys the realizability gate is pinned in
//! `commuting_statements_gate_test.rs`.

use debundle_e2e_support::*;

/// `globalThis.record` is a callable the analysis cannot see into, so every
/// call to it is an opaque effect: the `sequenced` edges between the callers
/// exist only as conservative ordering, which is what a premise may certify.
const SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nconst alpha = (globalThis.record(\"alpha\"), \"A\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nvar first = (globalThis.record(\"first\"), \"F\"), second = (globalThis.record(\"second\"), \"S\");\nglobalThis.record(\"boot\");\nglobalThis.record(\"anon\");\nglobalThis.record(\"anon\");\nconsole.log(alpha, beta, first, second, trace.slice().sort().join(\",\"));\nexport { alpha, beta, first, second };\n";
const RECORD_INSTALL: &str = "globalThis.record = (key) => { trace.push(key); };";
const ALPHA: &str = "const alpha = (globalThis.record(\"alpha\"), \"A\");";
const BETA: &str = "const beta = (globalThis.record(\"beta\"), \"B\");";
const FIRST_SECOND: &str = "var first = (globalThis.record(\"first\"), \"F\"), second = (globalThis.record(\"second\"), \"S\");";
const ENTRY: &str = "commuting_statements[0]";

fn opts(pairs: Vec<CommutingStatementsContract>) -> FixtureOpts<'static> {
    FixtureOpts::new(
        SOURCE,
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("trace"),
                Member::new("alpha"),
                Member::new("beta"),
                Member::new("first"),
                Member::new("second"),
            ],
            &[RECORD_INSTALL],
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_commuting_statements("static/app", pairs)
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

#[test]
fn valid_pairs_resolve_through_every_selector_form() {
    let pairs = vec![
        commuting_pair(
            declared_source_unit(ALPHA, "alpha"),
            member_source_unit("all", "beta"),
        ),
        commuting_pair(
            anonymous_source_unit("globalThis.record(\"boot\");"),
            declared_source_unit(FIRST_SECOND, "second"),
        ),
    ];
    let report = validate_json(opts(pairs.clone()));
    assert_eq!(report["validation"]["success"], true, "{report:#}");
    assert_preserved_entry_output(
        &run_fixture(opts(pairs)),
        "A B F S alpha,anon,anon,beta,boot,first,second\n",
    );
}

/// A repeated `var` is one binding with several declaration sites. A
/// `source_match` endpoint selects the site its template matched, not the
/// binding's first (representative) site that member selectors canonicalize
/// to.
#[test]
fn repeated_var_endpoint_selects_the_matched_declaration_site() {
    const SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nvar shared = (globalThis.record(\"one\"), 1);\nglobalThis.record(\"boot\");\nvar shared = (globalThis.record(\"two\"), 2);\nconsole.log(shared, trace.slice().sort().join(\",\"));\nexport { shared };\n";
    const SECOND_SITE: &str = "var shared = (globalThis.record(\"two\"), 2);";
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module_with_anon(
                "all",
                &[Member::new("trace"), Member::new("shared")],
                &[RECORD_INSTALL],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements(
            "static/app",
            vec![commuting_pair(
                anonymous_source_unit("globalThis.record(\"boot\");"),
                declared_source_unit(SECOND_SITE, "shared"),
            )],
        ),
    );
    assert_preserved_entry_output(&fixture, "2 boot,one,two\n");
    let graph = fixture.dependency_graph();
    let [entry] = graph.commuting_statements.as_slice() else {
        panic!("{:#?}", graph.commuting_statements);
    };
    let second_site = graph
        .nodes
        .iter()
        .filter(|node| node.declared_bindings.iter().any(|b| b.binding == "shared"))
        .max_by_key(|node| node.statement_ordinal)
        .expect("a declaration site of `shared`");
    assert_eq!(entry.b, second_site.id, "{graph:#?}");
}

#[test]
fn unresolved_endpoint_names_the_entry_and_endpoint() {
    expect_rejected(
        || {
            opts(vec![commuting_pair(
                declared_source_unit(ALPHA, "alpha"),
                declared_source_unit(
                    "const missing = (globalThis.record(\"missing\"), \"M\");",
                    "missing",
                ),
            )])
        },
        &[&format!("{ENTRY}.b")],
    );
    expect_rejected(
        || {
            opts(vec![commuting_pair(
                anonymous_source_unit("globalThis.record(\"boot\");"),
                serde_yaml::from_str(&format!(
                    "source_match:\n  match: {FIRST_SECOND:?}\n  bindings: [first, second]\n"
                ))
                .expect("valid selector"),
            )])
        },
        &[&format!("{ENTRY}.b"), "at most one declared binding"],
    );
}

#[test]
fn ambiguous_anonymous_endpoint_is_rejected() {
    expect_rejected(
        || {
            opts(vec![commuting_pair(
                anonymous_source_unit("globalThis.record(\"anon\");"),
                declared_source_unit(ALPHA, "alpha"),
            )])
        },
        &[&format!("{ENTRY}.a"), "ambiguous"],
    );
}

#[test]
fn same_source_unit_pair_is_rejected() {
    expect_rejected(
        || {
            opts(vec![commuting_pair(
                declared_source_unit(ALPHA, "alpha"),
                member_source_unit("all", "alpha"),
            )])
        },
        &[ENTRY, "same source unit"],
    );
}

#[test]
fn duplicate_unordered_pair_is_rejected() {
    expect_rejected(
        || {
            opts(vec![
                commuting_pair(
                    declared_source_unit(ALPHA, "alpha"),
                    declared_source_unit(BETA, "beta"),
                ),
                commuting_pair(
                    member_source_unit("all", "beta"),
                    member_source_unit("all", "alpha"),
                ),
            ])
        },
        &["commuting_statements[1] duplicates", ENTRY],
    );
}

#[test]
fn endpoint_in_another_chunk_is_rejected() {
    const CONSUMER: &str =
        "import { alias } from './peer.js';\nconsole.log(alias);\nconsole.log(1);\n";
    const PROVIDER: &str = "var alias = 1;\nexport { alias };\n";
    let peer_modules = [(
        "static/peer",
        vec![logical_module("peer", &[Member::new("alias")])],
    )];
    expect_rejected(
        || {
            FixtureOpts::new(CONSUMER, vec![])
                .with_extra_chunks(&[("static/peer", PROVIDER)])
                .with_extra_chunk_logical_modules(&peer_modules)
                .with_commuting_statements(
                    "static/app",
                    vec![commuting_pair(
                        anonymous_source_unit("console.log(1);"),
                        member_source_unit("peer", "alias"),
                    )],
                )
        },
        &[&format!("{ENTRY}.b"), "belongs to another provider"],
    );
}

/// Entries resolve only when their chunk's dependency graph is built, so an
/// entry for a chunk the run does not materialize is refused rather than never
/// checked.
#[test]
fn entry_for_an_unmaterialized_chunk_is_rejected() {
    const CONSUMER: &str = "import { alias } from './peer.js';\nconsole.log(alias);\n";
    const PROVIDER: &str = "var alias = 1;\nconsole.log(2);\nexport { alias };\n";
    expect_rejected(
        || {
            FixtureOpts::new(CONSUMER, vec![])
                .with_extra_chunks(&[("static/peer", PROVIDER)])
                .with_commuting_statements(
                    "static/peer",
                    vec![commuting_pair(
                        anonymous_source_unit("console.log(2);"),
                        declared_source_unit("var alias = 1;", "alias"),
                    )],
                )
        },
        &[
            "chunk_analysis_options.static/peer.commuting_statements",
            "does not materialize",
        ],
    );
}

#[test]
fn endpoint_requires_exactly_one_selector_variant() {
    let common = "b:\n  resolved_member: { module: all, name: beta }\n";
    for a in [
        "a: {}\n",
        "a:\n  source_match: { match: 'const alpha = 1;' }\n  resolved_member: { module: all, name: alpha }\n",
    ] {
        let error = serde_yaml::from_str::<CommutingStatementsContract>(&format!("{common}{a}"))
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("exactly one of source_match or resolved_member"),
            "{error}"
        );
    }
}
