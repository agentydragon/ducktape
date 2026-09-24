//! A free identifier in a `source_match` template that names a spec entity is
//! a reference: the template matches only where that identifier is the
//! entity's own binding. A name exported by the template's own module wins,
//! one exported by several other modules is `invalid`, and an unshadowed
//! runtime global must keep its spelling. Any other free name stays a
//! wildcard.

use debundle_e2e_support::{
    Fixture, FixtureOpts, Member, assert_module_source, find_outcome, logical_module,
    read_selector_outcomes, run_dry_run_rejection_fixture, run_fixture, run_source_only_validate,
    run_spec_validate, write_validate_fixture_spec,
};
use serde_json::{Value, json};

const TWO_CLASSES: &str = r#"class a {
  constructor(n) {
    this.n = n;
  }
}
class b {
  constructor(n) {
    this.n = "b" + n;
  }
}
const c = new b(1);
const d = new a(1);
console.log(c.n, d.n);
"#;

/// The same classes, with only an instance of the class `Widget` does not
/// select.
const OTHER_CLASS_ONLY: &str = r#"class a {
  constructor(n) {
    this.n = n;
  }
}
class b {
  constructor(n) {
    this.n = "b" + n;
  }
}
const c = new b(1);
console.log(new a(2).n, c.n);
"#;

const DEFAULT_WIDGET: &str = "const defaultWidget = new Widget(1);";

fn widget_fixture(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "widgets/widget",
                &[Member::source_alpha(
                    "Widget",
                    "class Widget {\n  constructor(n) {\n    this.n = n;\n  }\n}",
                )],
            ),
            logical_module(
                "widgets/default",
                &[Member::source_alpha("DefaultWidget", DEFAULT_WIDGET)],
            ),
        ],
    )
}

/// `new Widget(1)` matches both instances; only `d` constructs the class the
/// `Widget` selector resolved to.
#[test]
fn reference_to_a_source_match_entity_picks_the_agreeing_match() {
    let fixture = run_fixture(widget_fixture(TWO_CLASSES));
    assert_no_outcomes(&fixture);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/widgets/default.js",
        &["new Widget(1)"],
        &[],
    );
}

#[test]
fn no_match_agreeing_with_a_reference_is_a_conflict_with_it() {
    let expected = json!({
        "kind": "conflict",
        "with": [{"logical_module": "widgets/widget", "entity": {"export": "Widget"}}],
    });
    let rejected = run_dry_run_rejection_fixture(widget_fixture(OTHER_CLASS_ONLY));
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let default_widget = find_outcome(&outcomes, "conflict", "DefaultWidget");
    assert_eq!(default_widget["outcome"], expected, "{default_widget:#}");

    let validate = validate_json(widget_fixture(OTHER_CLASS_ONLY));
    assert_eq!(
        validate["outcomes"],
        json!([default_widget]),
        "{validate:#}"
    );
}

/// A name pin's binding is known without a solve: the template matches only
/// where its free name is that binding.
#[test]
fn reference_to_a_name_pin_matches_only_the_pinned_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const L = { log(m) { console.log("L" + m); } };
const M = { log(m) { console.log("M" + m); } };
function f() {
  L.log("x");
}
function g() {
  M.log("x");
}
f();
g();
"#,
        vec![
            logical_module("logging/logger", &[Member::renamed("logger", "L")]),
            logical_module(
                "logging/use",
                &[Member::source_alpha(
                    "UseLogger",
                    "function useLogger() {\n  logger.log(\"x\");\n}",
                )],
            ),
        ],
    ));
    assert_no_outcomes(&fixture);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/logging/use.js",
        &["logger.log(\"x\")"],
        &[],
    );
}

/// `Widget` is exported by two modules and the template's own module exports
/// neither, so which entity it names is undefined.
#[test]
fn name_exported_by_several_other_modules_is_invalid() {
    let fixture = || {
        FixtureOpts::new(
            TWO_CLASSES,
            vec![
                logical_module("widgets/a", &[Member::renamed("Widget", "a")]),
                logical_module("widgets/b", &[Member::renamed("Widget", "b")]),
                logical_module(
                    "widgets/default",
                    &[Member::source_alpha("DefaultWidget", DEFAULT_WIDGET)],
                ),
            ],
        )
    };
    let rejected = run_dry_run_rejection_fixture(fixture());
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let default_widget = find_outcome(&outcomes, "invalid", "DefaultWidget");
    assert_eq!(
        default_widget["outcome"]["error"],
        "ambiguous_reference: template identifier `Widget` is exported by modules widgets/a, \
         widgets/b; rename it in the template or rename one export",
        "{default_widget:#}"
    );

    let validate = validate_json(fixture());
    assert_eq!(
        validate["outcomes"],
        json!([default_widget]),
        "{validate:#}"
    );
}

/// The template's own module exports `Widget`, so another module's `Widget`
/// does not make the name ambiguous.
#[test]
fn own_module_export_wins_over_other_modules() {
    let fixture = run_fixture(FixtureOpts::new(
        TWO_CLASSES,
        vec![
            logical_module(
                "widgets/a",
                &[
                    Member::renamed("Widget", "a"),
                    Member::source_alpha("DefaultWidget", DEFAULT_WIDGET),
                ],
            ),
            logical_module("widgets/b", &[Member::renamed("Widget", "b")]),
        ],
    ));
    assert_no_outcomes(&fixture);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/widgets/a.js",
        &["DefaultWidget = new Widget(1)"],
        &[],
    );
}

/// A referenced name must bind one chunk identifier throughout the match:
/// `p` returns two different classes where the template returns `Widget`
/// twice.
#[test]
fn match_binding_a_reference_to_two_identifiers_is_dropped() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"class a {}
class b {}
function p() {
  return [() => a, () => b];
}
function q() {
  return [() => a, () => a];
}
console.log(p().length, q().length);
"#,
        vec![
            logical_module("widgets/widget", &[Member::renamed("Widget", "a")]),
            logical_module(
                "widgets/pair",
                &[Member::source_alpha(
                    "Pair",
                    "function pair() {\n  return [() => Widget, () => Widget];\n}",
                )],
            ),
        ],
    ));
    assert_no_outcomes(&fixture);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/widgets/pair.js",
        &["source bindings: q."],
        &[],
    );
}

const KEYS_CHUNK: &str = r#"const e = { keys() { return ["e"]; } };
function f(o) {
  return Object.keys(o);
}
function g(o) {
  return e.keys(o);
}
console.log(f({ x: 1 }), g({}));
"#;

/// `Object` is a runtime global the chunk does not declare, so it matches
/// only `Object`, not the chunk's `e`.
#[test]
fn unshadowed_global_keeps_its_spelling() {
    let fixture = run_fixture(FixtureOpts::new(
        KEYS_CHUNK,
        vec![logical_module(
            "keys/of",
            &[Member::source_alpha(
                "keysOf",
                "function keysOf(o) {\n  return Object.keys(o);\n}",
            )],
        )],
    ));
    assert_no_outcomes(&fixture);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/keys/of.js",
        &["Object.keys(o)"],
        &[],
    );
}

/// `helper` names no entity and no global: it binds whatever the chunk has.
#[test]
fn other_free_name_stays_a_wildcard() {
    let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(
        KEYS_CHUNK,
        vec![logical_module(
            "keys/any",
            &[Member::source_alpha(
                "anyKeys",
                "function anyKeys(o) {\n  return helper.keys(o);\n}",
            )],
        )],
    ));
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let any_keys = find_outcome(&outcomes, "ambiguous", "anyKeys");
    assert_eq!(
        any_keys["outcome"]["candidates"]
            .as_array()
            .map(|candidates| candidates.len()),
        Some(2),
        "{any_keys:#}"
    );
}

/// Every selector resolved on its own or through its references: nothing to
/// report, so no report is written.
fn assert_no_outcomes(fixture: &Fixture) {
    let report = fixture
        .report_root
        .join("static/app/selector_diagnostics.json");
    assert!(
        !report.exists(),
        "{}",
        std::fs::read_to_string(&report).unwrap()
    );
}

const KINDS_CHUNK: &str = r#"class a {}
const b = { x: 1 };
const e = { y: 2 };
const c = new a(Object.keys(b).length);
const d = e;
console.log(c, d);
"#;

const MADE: &str = "const made = new Widget(Object.keys(helper).length);";
const ALIAS: &str = "const alias = Shared;";

fn kinds_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        KINDS_CHUNK,
        vec![
            logical_module("kinds/widget", &[Member::renamed("Widget", "a")]),
            logical_module("kinds/x", &[Member::renamed("Shared", "b")]),
            logical_module("kinds/y", &[Member::renamed("Shared", "e")]),
            logical_module("kinds/made", &[Member::source_alpha("made", MADE)]),
            logical_module("kinds/alias", &[Member::source_alpha("alias", ALIAS)]),
        ],
    )
}

/// `validate` says what each free identifier of a matched template means: a
/// reference with its entity, a global, an ambiguous name with its exporters,
/// or a wildcard. Both modes agree; the text output counts them and lists
/// references and ambiguous names.
#[test]
fn validate_lists_free_identifiers_by_kind() {
    let expected = json!([
        {
            "logical_module": "kinds/alias",
            "exports": ["alias"],
            "identifiers": [{"name": "Shared", "kind": "ambiguous", "modules": ["kinds/x", "kinds/y"]}],
        },
        {
            "logical_module": "kinds/made",
            "exports": ["made"],
            "identifiers": [
                {"name": "Object", "kind": "global"},
                {
                    "name": "Widget",
                    "kind": "reference",
                    "entity": {"logical_module": "kinds/widget", "entity": {"export": "Widget"}},
                },
                {"name": "helper", "kind": "wildcard"},
            ],
        },
    ]);
    let without_chunk = |report: &Value| {
        let mut templates = report["templates"].clone();
        for template in templates.as_array_mut().unwrap() {
            template.as_object_mut().unwrap().remove("chunk");
        }
        templates
    };

    let spec = validate_json(kinds_fixture());
    assert_eq!(without_chunk(&spec), expected, "{spec:#}");
    assert_eq!(spec["templates"][0]["chunk"], "static/app", "{spec:#}");

    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    std::fs::write(&source, KINDS_CHUNK).unwrap();
    let modules = dir.path().join("modules");
    let module = |path: &str, body: String| {
        let file = modules.join(format!("{path}.yaml"));
        std::fs::create_dir_all(file.parent().unwrap()).unwrap();
        std::fs::write(file, body).unwrap();
    };
    let pin = |name: &str, binding: &str| {
        format!("members:\n  - name: {name}\n    selector: {{ binding: {{ name: {binding} }} }}\n")
    };
    let claim = |template: &str, local: &str| {
        format!("source_matches:\n  - match: '{template}'\n    bindings:\n      - {local}\n")
    };
    module("kinds/widget", pin("Widget", "a"));
    module("kinds/x", pin("Shared", "b"));
    module("kinds/y", pin("Shared", "e"));
    module("kinds/made", claim(MADE, "made"));
    module("kinds/alias", claim(ALIAS, "alias"));
    let out = run_source_only_validate(&modules, &source, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let source_only: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(without_chunk(&source_only), expected, "{source_only:#}");

    let fixture = write_validate_fixture_spec(kinds_fixture());
    let text = run_spec_validate(&fixture.spec_path, &["--format", "text"]);
    assert!(text.status.success(), "stderr={}", text.stderr);
    for line in [
        "  - static/app::kinds/alias `alias`: `Shared` is ambiguous: exported by kinds/x, kinds/y",
        "2 matched template(s) with free identifiers: ambiguous=1, global=1, reference=1, \
         wildcard=1",
        "  - static/app::kinds/made `made`: `Widget` references `Widget` in kinds/widget",
    ] {
        assert!(text.stdout.contains(line), "{line}\n{}", text.stdout);
    }
    assert!(!text.stdout.contains("`helper`"), "{}", text.stdout);
    assert!(!text.stdout.contains("`Object`"), "{}", text.stdout);
}

fn validate_json(opts: FixtureOpts<'_>) -> Value {
    let fixture = write_validate_fixture_spec(opts);
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(
        out.status.success(),
        "spec validate exited non-zero: stderr={}",
        out.stderr
    );
    serde_json::from_str(&out.stdout)
        .unwrap_or_else(|err| panic!("parse validate json: {err}\nstdout:\n{}", out.stdout))
}
