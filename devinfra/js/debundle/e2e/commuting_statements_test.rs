//! `chunk_analysis_options.<chunk>.commuting_statements` resolves each endpoint
//! to exactly one source unit of its chunk and rejects malformed pairs. The
//! gate does not consume the pairs yet, so a valid spec is accepted exactly
//! when it is accepted without them.

use debundle_e2e_support::*;

const SOURCE: &str = "const registry = [];\nfunction factory(value) { registry.push(value); return value; }\nconst a = factory(1);\nconst b = factory(2);\nvar first = factory(3), second = factory(4);\nregistry.push(0);\nregistry.push(0);\nconsole.log(a, b, first, second, registry.length);\nexport { a, b, factory, first, second };\n";
const ENTRY: &str = "commuting_statements[0]";

fn selector(yaml: &str) -> SourceUnitSelector {
    serde_yaml::from_str(yaml).expect("valid endpoint selector")
}

fn anonymous(statement: &str) -> SourceUnitSelector {
    selector(&format!("source_match:\n  match: {statement:?}\n"))
}

fn declared(statement: &str, binding: &str) -> SourceUnitSelector {
    selector(&format!(
        "source_match:\n  match: {statement:?}\n  bindings: [{binding}]\n"
    ))
}

fn member(module: &str, name: &str) -> SourceUnitSelector {
    selector(&format!(
        "resolved_member:\n  module: {module}\n  name: {name}\n"
    ))
}

fn pair(a: SourceUnitSelector, b: SourceUnitSelector) -> CommutingStatementsContract {
    CommutingStatementsContract {
        a,
        b,
        note: Some("Neutral fixture; neither statement observes the other.".to_owned()),
    }
}

fn opts(pairs: Vec<CommutingStatementsContract>) -> FixtureOpts<'static> {
    FixtureOpts::new(
        SOURCE,
        vec![logical_module(
            "all",
            &[
                Member::new("registry"),
                Member::new("factory"),
                Member::new("a"),
                Member::new("b"),
                Member::new("first"),
                Member::new("second"),
            ],
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
        pair(declared("const a = factory(1);", "a"), member("all", "b")),
        pair(
            anonymous("console.log(a, b, first, second, registry.length);"),
            declared("var first = factory(3), second = factory(4);", "second"),
        ),
    ];
    let report = validate_json(opts(pairs.clone()));
    assert_eq!(report["validation"]["success"], true, "{report:#}");
    assert_preserved_entry_output(&run_fixture(opts(pairs)), "1 2 3 4 6\n");
}

#[test]
fn unresolved_endpoint_names_the_entry_and_endpoint() {
    expect_rejected(
        || {
            opts(vec![pair(
                declared("const a = factory(1);", "a"),
                declared("const missing = factory(9);", "missing"),
            )])
        },
        &[&format!("{ENTRY}.b")],
    );
    expect_rejected(
        || {
            opts(vec![pair(
                anonymous("console.log(a, b, first, second, registry.length);"),
                selector(
                    "source_match:\n  match: \"var first = factory(3), second = factory(4);\"\n  bindings: [first, second]\n",
                ),
            )])
        },
        &[&format!("{ENTRY}.b"), "at most one declared binding"],
    );
}

#[test]
fn ambiguous_anonymous_endpoint_is_rejected() {
    expect_rejected(
        || {
            opts(vec![pair(
                anonymous("registry.push(0);"),
                declared("const a = factory(1);", "a"),
            )])
        },
        &[&format!("{ENTRY}.a"), "ambiguous"],
    );
}

#[test]
fn same_source_unit_pair_is_rejected() {
    expect_rejected(
        || {
            opts(vec![pair(
                declared("const a = factory(1);", "a"),
                member("all", "a"),
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
                pair(
                    declared("const a = factory(1);", "a"),
                    declared("const b = factory(2);", "b"),
                ),
                pair(member("all", "b"), member("all", "a")),
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
                    vec![pair(anonymous("console.log(1);"), member("peer", "alias"))],
                )
        },
        &[&format!("{ENTRY}.b"), "belongs to another provider"],
    );
}

#[test]
fn endpoint_requires_exactly_one_selector_variant() {
    let common = "b:\n  resolved_member: { module: all, name: b }\n";
    for a in [
        "a: {}\n",
        "a:\n  source_match: { match: 'const a = factory(1);' }\n  resolved_member: { module: all, name: a }\n",
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
