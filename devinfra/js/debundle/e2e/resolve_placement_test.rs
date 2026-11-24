//! A resolved selector assignment is input to a checker, not its verdict.
use debundle_e2e_support::saved_analysis::{command, rejected_split};
use debundle_e2e_support::*;
use serde_json::{Value, json};
use std::fs;

fn resolve(opts: FixtureOpts<'_>, subset: Option<Value>) -> (CommandResult, Option<Value>) {
    let fixture = write_validate_fixture_spec(opts);
    let root = fixture.workspace();
    let out = root.path().join("resolved.json");
    let subset_path = root.path().join("subset.json");
    let mut args = vec![
        "spec",
        "resolve-placement",
        "--spec",
        fixture.spec_path.to_str().unwrap(),
        "--chunk",
        "static/app",
        "--out",
        out.to_str().unwrap(),
    ];
    if let Some(subset) = subset {
        fs::write(&subset_path, subset.to_string()).unwrap();
        args.extend(["--ownership-subset", subset_path.to_str().unwrap()]);
    }
    let run = command(&args);
    let result = out
        .exists()
        .then(|| serde_json::from_slice(&fs::read(out).unwrap()).unwrap());
    (run, result)
}

#[test]
fn unambiguous_interleaved_ownership_survives_gate_rejection() {
    run_rejection_fixture(rejected_split());
    let opts = rejected_split();
    let hash = source_sha256(opts.source);
    let (run, output) = resolve(opts, None);
    assert!(run.status.success(), "{}", run.stderr);
    assert_eq!(
        output.unwrap(),
        json!({"chunk_id":"static/app","source_sha256":hash,"import_order":["mod_a","mod_b"],"units":[
            {"ordinal":0,"owner":null},{"ordinal":1,"owner":"mod_a","members":{"A":"A"}},
            {"ordinal":2,"owner":"mod_b","members":{"B":"B"}},{"ordinal":3,"owner":"mod_a","members":{"C":"C"}},
            {"ordinal":4,"owner":null},{"ordinal":5,"owner":null}
        ]})
    );
}

#[test]
fn post_split_ordinals_preserve_destructuring_and_anonymous_claims() {
    let source =
        "const first=0, { x: left, y: right } = globalThis.point, last=2;\nconsole.log(last);\n";
    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "point",
                &[
                    Member::renamed("horizontal", "left"),
                    Member::renamed("vertical", "right"),
                ],
                &["console.log(last);"],
            ),
            logical_module("rest", &[Member::new("last")]),
        ],
    );
    let (run, output) = resolve(
        opts,
        Some(json!({"modules":[{"chunk":"static/app","module":"point"}]})),
    );
    assert!(run.status.success(), "{}", run.stderr);
    assert_eq!(
        output.unwrap(),
        json!({"chunk_id":"static/app","source_sha256":source_sha256(source),"import_order":["point"],"units":[
            {"ordinal":0,"owner":null},{"ordinal":1,"owner":"point","members":{"left":"horizontal","right":"vertical"}},
            {"ordinal":2,"owner":null},{"ordinal":3,"owner":"point"}
        ]})
    );
}

#[test]
fn even_filtered_out_selectors_must_resolve() {
    let opts = FixtureOpts::new(
        "const value = 1;",
        vec![
            logical_module("found", &[Member::new("value")]),
            logical_module("missing", &[Member::new("absent")]),
        ],
    );
    let (run, output) = resolve(
        opts,
        Some(json!({"modules":[{"chunk":"static/app","module":"found"}]})),
    );
    assert!(!run.status.success());
    assert!(run.stderr.contains("absent"), "{}", run.stderr);
    assert!(output.is_none());
}

#[test]
fn rejects_conflicting_owners_of_one_destructuring_unit_before_filtering() {
    let opts = FixtureOpts::new(
        "const { x, y } = globalThis.point;",
        vec![
            logical_module("a", &[Member::new("x")]),
            logical_module("b", &[Member::new("y")]),
        ],
    );
    let (run, output) = resolve(
        opts,
        Some(json!({"modules":[{"chunk":"static/app","module":"a"}]})),
    );
    assert!(!run.status.success());
    assert!(run.stderr.contains("inconsistent claims"), "{}", run.stderr);
    assert!(output.is_none());
}

#[test]
fn ambiguous_source_selector_fails_instead_of_picking_the_first_match() {
    let opts = FixtureOpts::new(
        "const left = () => 2; const right = () => 2;",
        vec![logical_module(
            "candidate",
            &[Member::source_alpha("two", "const chosen = () => 2;")],
        )],
    );
    let (run, output) = resolve(opts, None);
    assert!(!run.status.success());
    assert!(run.stderr.contains("two"), "{}", run.stderr);
    assert!(output.is_none());
}
