//! E2E coverage for the ownership witnesses attached to unsatisfiable selector
//! groups. These fixtures drive the real CLI with small synthetic JavaScript.

use debundle_e2e_support::{
    BindingGroup, FixtureOpts, GraphFixture, Member, find_outcome, logical_module,
    logical_module_with_binding_groups, outcomes, read_selector_outcomes,
    run_dry_run_rejection_fixture, run_source_only_validate, validate_json,
};
use serde_json::{Value, json};

fn duplicate_singletons_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        "function actual() { return \"shared\"; }\n",
        vec![
            logical_module(
                "conflicts/left",
                &[Member::source_alpha(
                    "Left",
                    "function selected() { return \"shared\"; }",
                )],
            ),
            logical_module(
                "conflicts/right",
                &[Member::source_alpha(
                    "Right",
                    "function selected() { return \"shared\"; }",
                )],
            ),
        ],
    )
}

fn witness(record: &Value) -> &Value {
    record["outcome"]["witness"]
        .as_object()
        .map(|_| &record["outcome"]["witness"])
        .unwrap_or_else(|| panic!("expected an ownership witness: {record:#}"))
}

#[test]
fn duplicate_singletons_report_the_same_group_and_ownership_witness() {
    let rejected = run_dry_run_rejection_fixture(duplicate_singletons_fixture());
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let left = find_outcome(&outcomes, "unsatisfiable", "Left");
    let right = find_outcome(&outcomes, "unsatisfiable", "Right");
    let expected = json!({
        "kind": "unsatisfiable",
        "group": {"logical_module": "conflicts/left", "entity": {"export": "Left"}},
        "witness": {
            "selectors": [
                {"logical_module": "conflicts/left", "entity": {"export": "Left"}},
                {"logical_module": "conflicts/right", "entity": {"export": "Right"}},
            ],
            "owners": [{"owner": 0, "statement": 0, "bindings": ["actual"]}],
        },
    });
    assert_eq!(left["outcome"], expected, "{left:#}");
    assert_eq!(right["outcome"], expected, "{right:#}");
    assert!(
        rejected
            .stderr
            .contains("participates in ownership conflict"),
        "{}",
        rejected.stderr
    );

    let validate = validate_json(duplicate_singletons_fixture());
    assert_eq!(validate["outcomes"], json!([left, right]), "{validate:#}");
}

#[test]
fn three_selectors_over_two_owners_report_a_hall_witness() {
    let source = r#"function first() { return "shared"; }
function second() { return "shared"; }
"#;
    let modules = [
        ("hall/one", "One"),
        ("hall/two", "Two"),
        ("hall/three", "Three"),
    ]
    .into_iter()
    .map(|(path, export)| {
        logical_module(
            path,
            &[Member::source_alpha(
                export,
                "function selected() { return EXPR; }",
            )],
        )
    })
    .collect();
    let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(source, modules));
    let outcomes = read_selector_outcomes(&rejected.report_root);

    for export in ["One", "Two", "Three"] {
        let record = find_outcome(&outcomes, "unsatisfiable", export);
        assert_eq!(
            record["outcome"]["group"]["logical_module"], "hall/one",
            "{record:#}"
        );
        let conflict = witness(record);
        assert_eq!(
            conflict["selectors"].as_array().unwrap().len(),
            3,
            "{record:#}"
        );
        assert_eq!(
            conflict["selectors"]
                .as_array()
                .unwrap()
                .iter()
                .map(|selector| selector["logical_module"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["hall/one", "hall/three", "hall/two"],
            "{record:#}"
        );
        assert_eq!(
            conflict["owners"],
            json!([
                {"owner": 0, "statement": 0, "bindings": ["first"]},
                {"owner": 1, "statement": 1, "bindings": ["second"]},
            ]),
            "{record:#}"
        );
    }
}

fn chain_with_bystander_and_independent_resolution() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"function x() { return "x"; }
function y() { return "y"; }
function z() {
  const marker = 1;
  return "z";
}
const u = 1;
const v = 2;
console.log(x(), y(), z(), u, v);
"#,
        vec![
            logical_module(
                "chain/a",
                &[Member::source_alpha(
                    "A",
                    "function selected() { return \"x\"; }",
                )],
            ),
            logical_module(
                "chain/b",
                &[Member::source_alpha(
                    "B",
                    "function selected() { return EXPR; }",
                )],
            ),
            logical_module(
                "chain/c",
                &[Member::source_alpha(
                    "C",
                    "function selected() { return \"y\"; }",
                )],
            ),
            logical_module(
                "chain/bystander",
                &[Member::source_alpha(
                    "Bystander",
                    "function selected() { STMT_LIST; }",
                )],
            ),
            logical_module(
                "resolved/either",
                &[Member::source_alpha("Either", "const selected = EXPR;")],
            ),
            logical_module(
                "resolved/only-v",
                &[Member::source_alpha("OnlyV", "const selected = 2;")],
            ),
        ],
    )
}

#[test]
fn deficient_chain_explains_participants_and_blocked_bystander() {
    let rejected = run_dry_run_rejection_fixture(chain_with_bystander_and_independent_resolution());
    let outcomes = read_selector_outcomes(&rejected.report_root);

    let a = find_outcome(&outcomes, "unsatisfiable", "A");
    let b = find_outcome(&outcomes, "unsatisfiable", "B");
    let c = find_outcome(&outcomes, "unsatisfiable", "C");
    let bystander = find_outcome(&outcomes, "unsatisfiable", "Bystander");
    let chain_witness = witness(a);
    assert_eq!(
        chain_witness["selectors"],
        json!([
            {"logical_module": "chain/a", "entity": {"export": "A"}},
            {"logical_module": "chain/b", "entity": {"export": "B"}},
            {"logical_module": "chain/c", "entity": {"export": "C"}},
        ]),
        "{a:#}"
    );
    for record in [b, c, bystander] {
        assert_eq!(&record["outcome"]["witness"], chain_witness, "{record:#}");
    }
    assert!(
        !chain_witness["selectors"]
            .as_array()
            .unwrap()
            .iter()
            .any(|selector| selector["logical_module"] == "chain/bystander"),
        "bystander must be outside the sufficient witness: {chain_witness:#}"
    );
    let a_line = rejected
        .stderr
        .lines()
        .find(|line| line.contains("as `A`"))
        .unwrap();
    let bystander_line = rejected
        .stderr
        .lines()
        .find(|line| line.contains("as `Bystander`"))
        .unwrap();
    assert!(
        a_line.contains("participates in ownership conflict"),
        "{a_line}"
    );
    assert!(
        bystander_line.contains("blocked by ownership conflict"),
        "{bystander_line}"
    );

    let either = find_outcome(&outcomes, "resolved", "Either");
    assert_eq!(
        either["outcome"],
        json!({
            "kind": "resolved",
            "owner": 3,
            "binding": "u",
            "resolved_by": {
                "by": "elimination",
                "claimers": [{"logical_module": "resolved/only-v", "entity": {"export": "OnlyV"}}],
            },
        }),
        "{either:#}"
    );
}

#[test]
fn reference_narrowing_does_not_make_an_unsound_ownership_witness() {
    let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(
        r#"const state = { nextUniqueId: 0 };
function first() { return state.nextUniqueId++; }
function second() { return state.nextUniqueId++; }
const a = () => first();
const b = () => second();
console.log(a(), b());
"#,
        vec![
            logical_module(
                "narrowed/anchor",
                &[Member::reads_member(
                    "generateId",
                    "nextUniqueId",
                    None,
                    Some("function_declaration"),
                )],
            ),
            logical_module(
                "narrowed/use-a",
                &[Member::source_alpha(
                    "UseA",
                    "const use = () => generateId();",
                )],
            ),
            logical_module(
                "narrowed/use-b",
                &[Member::source_alpha(
                    "UseB",
                    "const use = () => generateId();",
                )],
            ),
        ],
    ));
    let outcomes = read_selector_outcomes(&rejected.report_root);
    for export in ["generateId", "UseA", "UseB"] {
        let record = find_outcome(&outcomes, "unsatisfiable", export);
        assert!(record["outcome"].get("witness").is_none(), "{record:#}");
    }
    assert!(
        rejected
            .stderr
            .contains("no smaller ownership witness found"),
        "relational contradiction should use the no-witness explanation:\n{}",
        rejected.stderr
    );
}

#[test]
fn multi_binding_groups_contribute_one_exclusivity_representative() {
    let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(
        "const a = 0, b = 0;\nconsole.log(a, b);\n",
        vec![
            logical_module_with_binding_groups(
                "multi/left",
                &[],
                &[BindingGroup::source_alpha(
                    "const first = 0, second = 0;",
                    &[("first", "LeftOne"), ("second", "LeftTwo")],
                )],
            ),
            logical_module_with_binding_groups(
                "multi/right",
                &[],
                &[BindingGroup::source_alpha(
                    "const first = 0, second = 0;",
                    &[("first", "RightOne"), ("second", "RightTwo")],
                )],
            ),
        ],
    ));
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let first = find_outcome(&outcomes, "unsatisfiable", "LeftOne");
    let conflict = witness(first);
    assert_eq!(
        conflict["selectors"],
        json!([
            {"logical_module": "multi/left", "entity": {"export": "LeftOne"}},
            {"logical_module": "multi/right", "entity": {"export": "RightOne"}},
        ]),
        "grouped bindings from one source match are one ownership claim: {first:#}"
    );
    assert_eq!(
        conflict["owners"],
        json!([{"owner": 0, "statement": 0, "bindings": ["a"]}]),
        "the witness should use each source group's existing exclusivity representative: {first:#}"
    );
    for export in ["LeftTwo", "RightOne", "RightTwo"] {
        assert_eq!(
            &find_outcome(&outcomes, "unsatisfiable", export)["outcome"]["witness"],
            conflict,
            "all members share one group diagnostic"
        );
    }
}

fn without_chunk(record: &Value) -> Value {
    let mut record = record.clone();
    record
        .as_object_mut()
        .expect("selector outcome is an object")
        .remove("chunk");
    record
}

#[test]
fn source_only_validate_preserves_the_serialized_witness() {
    const SOURCE: &str = "function actual() { return \"shared\"; }\n";
    let tree = GraphFixture::rejected(
        SOURCE,
        &[
            (
                "conflicts/left.yaml",
                "source_matches:\n  - match: 'function selected() { return \"shared\"; }'\n    bindings:\n      - local: selected\n        name: Left\n",
            ),
            (
                "conflicts/right.yaml",
                "source_matches:\n  - match: 'function selected() { return \"shared\"; }'\n    bindings:\n      - local: selected\n        name: Right\n",
            ),
        ],
    );
    let source_only =
        run_source_only_validate(&tree.modules, &tree.source_path(), &["--format", "json"]);
    assert!(
        source_only.status.success(),
        "stderr: {}",
        source_only.stderr
    );
    let source_report: Value = serde_json::from_str(&source_only.stdout).unwrap_or_else(|error| {
        panic!("invalid source-only JSON ({error}): {}", source_only.stdout)
    });
    let source_records = outcomes(&source_report)
        .iter()
        .map(without_chunk)
        .collect::<Vec<_>>();

    let spec_report = validate_json(duplicate_singletons_fixture());
    let spec_records = outcomes(&spec_report)
        .iter()
        .map(without_chunk)
        .collect::<Vec<_>>();
    assert_eq!(
        source_records, spec_records,
        "source-only and --spec validation disagree"
    );
}

#[test]
fn oversized_group_keeps_a_bounded_group_level_diagnostic() {
    let modules = (0..257)
        .map(|index| {
            logical_module(
                &format!("claims/{index:03}"),
                &[Member::source_alpha(
                    "Selected",
                    "function selected() { return 1; }",
                )],
            )
        })
        .collect();
    let report = validate_json(FixtureOpts::new("function actual() { return 1; }", modules));
    assert_eq!(outcomes(&report).len(), 257);
    for record in outcomes(&report) {
        assert_eq!(record["outcome"]["kind"], "unsatisfiable");
        assert!(record["outcome"].get("witness").is_none());
        assert_eq!(record["outcome"]["group"]["logical_module"], "claims/000");
    }
}

#[test]
fn settled_reference_singletons_do_not_omit_their_premise_from_a_witness() {
    let report = validate_json(FixtureOpts::new(
        "function first() { return 1; } function second() { return 2; } \
         const a = () => first(); const b = () => second();",
        vec![
            logical_module(
                "anchor",
                &[Member::source_alpha(
                    "Target",
                    "function selected() { return 1; }",
                )],
            ),
            logical_module(
                "use_a",
                &[Member::source_alpha("UseA", "const use = () => Target();")],
            ),
            logical_module(
                "use_b",
                &[Member::source_alpha("UseB", "const use = () => Target();")],
            ),
        ],
    ));
    // Both use selectors become singleton {a} only because Target selects
    // first. Without that premise their original domains {a,b} are compatible.
    // Do not falsely claim that UseA and UseB alone form a contradiction.
    for name in ["UseA", "UseB"] {
        let record = find_outcome(outcomes(&report), "unsatisfiable", name);
        assert!(record["outcome"].get("witness").is_none(), "{record:#}");
    }
}
