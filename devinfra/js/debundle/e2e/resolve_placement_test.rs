//! A resolved selector assignment is input to a checker, not its verdict.
use debundle_e2e_support::saved_analysis::{command, command_with, rejected_split};
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
    let cli = command(&args);
    let cli_bytes = fs::read(&out).ok();
    if cli_bytes.is_some() {
        fs::remove_file(&out).unwrap();
    }
    let run = command_with(&resolver_path(), &args[2..]);
    assert_eq!(run.status.code(), cli.status.code(), "{}", run.stderr);
    assert_eq!(fs::read(&out).ok(), cli_bytes);
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

/// Exercise the CLI with fragments and both output files, including refusals.
fn resolve_fragments(
    source: &str,
    additions: &[(&str, &str)],
    subset: Value,
) -> (CommandResult, Option<Value>, Option<Value>) {
    let opts = FixtureOpts::new(
        source,
        vec![logical_module("shared", &[Member::new("seed")])],
    );
    let fixture = write_validate_fixture_spec(opts);
    let workspace = fixture.workspace();
    let dir = workspace.path();
    let full = dir.join("full.json");
    let selected = dir.join("selected.json");
    let view = dir.join("view.json");
    fs::write(&view, subset.to_string()).unwrap();
    let mut fragments = vec![];
    for (i, (owner, yaml)) in additions.iter().enumerate() {
        let file = dir.join(format!("fragment{i}.yaml"));
        fs::write(&file, yaml).unwrap();
        fragments.push(format!("{owner}={}", file.display()));
    }
    let mut args = vec![
        "spec",
        "resolve-placement",
        "--spec",
        fixture.spec_path.to_str().unwrap(),
        "--chunk",
        "static/app",
        "--ownership-subset",
        view.to_str().unwrap(),
        "--canonical-out",
        full.to_str().unwrap(),
        "--out",
        selected.to_str().unwrap(),
    ];
    for fragment in &fragments {
        args.extend(["--proposal-fragment", fragment.as_str()]);
    }
    let run = command(&args);
    let read = |p| {
        fs::read(p)
            .ok()
            .map(|b| serde_json::from_slice(&b).unwrap())
    };
    (run, read(full), read(selected))
}

const EXTRA: &str = r#"source_matches:
  - match: "const local = 7;"
    bindings: [{ local: local, name: extra }]
"#;
const ARROW: &str = r#"source_matches:
  - match: "const local = () => 1;"
    bindings: [{ local: local, name: other }]
"#;

fn keep_extra() -> Value {
    json!({"modules":[{"chunk":"static/app","module":"shared","bindings":["extra"]}]})
}

#[test]
fn append_to_existing_and_new_modules_then_filter_the_joint_solve() {
    let source = "const seed = 0; const value = 7; const second = () => 1;";
    let (run, full, selected) =
        resolve_fragments(source, &[("shared", EXTRA), ("new", ARROW)], keep_extra());
    assert!(run.status.success(), "{}", run.stderr);
    let full = full.unwrap();
    let selected = selected.unwrap();
    assert_eq!(full["source_sha256"], source_sha256(source));
    assert_eq!(selected["source_sha256"], full["source_sha256"]);
    assert_eq!(
        full["units"],
        json!([
            {"ordinal":0,"owner":"shared","members":{"seed":"seed"}},
            {"ordinal":1,"owner":"shared","members":{"value":"extra"}},
            {"ordinal":2,"owner":"new","members":{"second":"other"}}
        ])
    );
    assert_eq!(
        selected["units"],
        json!([
            {"ordinal":0,"owner":null}, full["units"][1], {"ordinal":2,"owner":null}
        ])
    );
    assert_eq!(full["import_order"], json!(["shared", "new"]));
    assert_eq!(selected["import_order"], json!(["shared"]));
}

#[test]
fn batch_resolves_multiple_chunks_once_with_qualified_fragments_and_both_views() {
    let secondary_modules = [(
        "static/secondary",
        vec![logical_module("shared", &[Member::new("seed")])],
    )];
    let opts = FixtureOpts::new(
        "const seed = 0;",
        vec![logical_module("shared", &[Member::new("seed")])],
    )
    .with_extra_chunks(&[("static/secondary", "const seed = 9; const local = 7;")])
    .with_extra_chunk_logical_modules(&secondary_modules);
    let fixture = write_validate_fixture_spec(opts);
    let root = fixture.workspace();
    let dir = root.path();
    let fragment = dir.join("fragment.yaml");
    let subset = dir.join("subset.json");
    let selected = dir.join("selected.json");
    let canonical = dir.join("canonical.json");
    fs::write(&fragment, EXTRA).unwrap();
    fs::write(
        &subset,
        json!({"modules":[{"chunk":"static/secondary","module":"added"}]}).to_string(),
    )
    .unwrap();
    let run = command(&[
        "spec",
        "resolve-placements",
        "--spec",
        fixture.spec_path.to_str().unwrap(),
        "--chunk",
        "static/app",
        "--chunk",
        "static/secondary",
        "--proposal-fragment",
        &format!("static/secondary::added={}", fragment.display()),
        "--ownership-subset",
        subset.to_str().unwrap(),
        "--canonical-out",
        canonical.to_str().unwrap(),
        "--out",
        selected.to_str().unwrap(),
    ]);
    assert!(run.status.success(), "{}", run.stderr);
    let selected: Value = serde_json::from_slice(&fs::read(selected).unwrap()).unwrap();
    let canonical: Value = serde_json::from_slice(&fs::read(canonical).unwrap()).unwrap();
    assert_eq!(selected.as_object().unwrap().len(), 1);
    assert_eq!(canonical.as_object().unwrap().len(), 1);
    assert_eq!(
        canonical["placements"],
        json!([
            {
                "chunk_id":"static/app",
                "source_sha256":source_sha256("const seed = 0;"),
                "import_order":["shared"],
                "units":[{"ordinal":0,"owner":"shared","members":{"seed":"seed"}}]
            },
            {
                "chunk_id":"static/secondary",
                "source_sha256":source_sha256("const seed = 9; const local = 7;"),
                "import_order":["shared","added"],
                "units":[
                    {"ordinal":0,"owner":"shared","members":{"seed":"seed"}},
                    {"ordinal":1,"owner":"added","members":{"local":"extra"}}
                ]
            }
        ])
    );
    assert_eq!(
        selected["placements"],
        json!([
            {
                "chunk_id":"static/app",
                "source_sha256":source_sha256("const seed = 0;"),
                "import_order":[],
                "units":[{"ordinal":0,"owner":null}]
            },
            {
                "chunk_id":"static/secondary",
                "source_sha256":source_sha256("const seed = 9; const local = 7;"),
                "import_order":["added"],
                "units":[
                    {"ordinal":0,"owner":null},
                    {"ordinal":1,"owner":"added","members":{"local":"extra"}}
                ]
            }
        ])
    );
}

#[test]
fn ambiguous_excluded_fragment_rejects_both_outputs() {
    let (run, full, selected) = resolve_fragments(
        "const seed = 0; const value = 7; const left = () => 1; const right = () => 1;",
        &[("shared", EXTRA), ("new", ARROW)],
        keep_extra(),
    );
    assert!(!run.status.success());
    assert!(run.stderr.contains("other"), "{}", run.stderr);
    assert!(full.is_none() && selected.is_none());
}

#[test]
fn fragments_cannot_replace_metadata_duplicate_claims_or_alias_owner_paths() {
    for (fragments, expected) in [
        (
            vec![("shared", "source_matches: []\nannotations: {}")],
            "unknown field",
        ),
        (
            vec![("shared", "source_matches: []")],
            "empty selector fragment",
        ),
        (vec![("shared", EXTRA), ("shared", EXTRA)], "duplicate"),
        (vec![("/shared", EXTRA)], "canonical proposal owner"),
    ] {
        let (run, full, selected) =
            resolve_fragments("const seed = 0; const x = 7;", &fragments, keep_extra());
        assert!(!run.status.success(), "should reject {expected}");
        assert!(run.stderr.contains(expected), "{}", run.stderr);
        assert!(full.is_none() && selected.is_none());
    }
}
