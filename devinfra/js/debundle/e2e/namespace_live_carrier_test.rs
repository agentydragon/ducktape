//! Real-CLI placement checks for namespace objects and default exports.
//!
//! These fixtures use source chunks and logical-module placements so each
//! accepted case compares the original ESM program with the emitted Node
//! program. Refused cyclic cases inspect the gate's structured cycle report;
//! their diagnostic output tree is never treated as a successful emission.

use debundle_e2e_support::*;

#[test]
fn foreign_fallback_keeps_its_source_when_its_ordinal_exceeds_the_callers_table() {
    let source = "import { run } from './peer.js';\nglobalThis.identity = value => value;\nconst result = run();\nconsole.log(result);\n";
    let peer = "const a=0; const b=0; const c=0; const d=0; const e=0; const f=0; const g=0; const h=0; const i=0;\nexport function run() { return globalThis.identity('ok'); }\n";
    let fixture = ProgramPlacement::new(source)
        .with_unassigned_mode(unassigned_mode_inline())
        .add_chunk("static/peer", peer, vec![])
        .run_with_graphs();
    assert_preserved_entry_output(&fixture, "ok\n");
    let graph: serde_json::Value = read_gzip_json(
        &fixture
            .report_root
            .join("static/app/dependency_graph.json.gz"),
    );
    let losses: Vec<_> = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|node| {
            node["synchronous_execution"]["precision_losses"]
                .as_array()
                .into_iter()
                .flatten()
        })
        .filter(|loss| loss["source_module"] == "static/peer")
        .collect();
    assert!(
        !losses.is_empty(),
        "foreign fallback lost its defining module: {graph:#}"
    );
    assert!(
        losses.iter().all(|loss| loss["statement_ordinal"] == 9
            && loss["source_location"]["source_path"]
                .as_str()
                .is_some_and(|path| path.ends_with("static/peer.js"))),
        "{losses:#?}"
    );
}

#[test]
fn anonymous_default_factory_keeps_returned_method_captures_across_export_routes() {
    for (imports, factory) in [
        ("import make, { node } from './peer.js';", "make"),
        ("import { make, node } from './relay.js';", "make"),
        (
            "import * as peer from './peer.js';\nconst make = peer.default;\nconst node = peer.node;",
            "make",
        ),
    ] {
        let source = format!(
            "{imports}\nconst record = {factory}(() => later);\nconst read = record.read;\nconst icon = node('svg');\nconst later = () => icon;\nconsole.log(read()());\n"
        );
        let fixture = ProgramPlacement::new(&source)
            .place_in_entry(logical_module("icons", &[Member::new("icon")]))
            .add_chunk("static/peer", "export default function(callback) { return { read() { return callback(); } }; }\nexport const node = tag => tag;\n", vec![])
            .add_chunk("static/relay", "export { default as make, node } from './peer.js';\n", vec![])
            .run();
        assert_preserved_entry_output(&fixture, "svg\n");
        assert_module_exports(
            &fixture.out_root,
            "static/app/modules/icons.js",
            &["icon"],
            &["make", "read", "later"],
        );
    }
}

#[test]
fn namespace_passed_returned_and_aliased_keeps_live_members_and_missing_fields() {
    let source = r#"import * as peer from "./peer.js";
const alias = peer;
const returned = peer.keep(alias);
const selected = returned.field;
const before = returned.count;
const changed = returned.next();
const after = alias.count;
const absent = returned.absent;
console.log(
  alias === peer,
  returned === peer,
  selected(),
  before,
  changed,
  after,
  absent === undefined,
  Object.keys(returned).sort().join(","),
);
"#;
    let peer = r#"let count = 0;
export { count };
export const field = () => "callable";
export const keep = value => value;
export const next = () => ++count;
"#;
    let fixture = ProgramPlacement::new(source)
        .with_unassigned_mode(unassigned_mode_inline())
        .place_in_entry(logical_module(
            "namespace_carrier",
            &[
                Member::new("alias"),
                Member::new("returned"),
                Member::new("selected"),
                Member::new("before"),
                Member::new("changed"),
                Member::new("after"),
                Member::new("absent"),
            ],
        ))
        .add_chunk("static/peer", peer, vec![])
        .run();

    assert_preserved_entry_output(
        &fixture,
        "true true callable 0 1 1 true count,field,keep,next\n",
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/namespace_carrier.js",
        &[
            "alias", "returned", "selected", "before", "changed", "after", "absent",
        ],
        &[],
    );
}

#[test]
fn namespace_escape_via_timer_keeps_later_callable_effects() {
    let source = r#"import "./peer.js";
export const later = () => {
  console.log("later");
  return "ready";
};
console.log("entry");
"#;
    let peer = r#"import * as app from "./app.js";
setTimeout(namespace => console.log(namespace.later()), 0, app);
"#;
    let fixture = ProgramPlacement::new(source)
        .with_unassigned_mode(unassigned_mode_inline())
        .place_in_entry(logical_module("later", &[Member::new("later")]))
        .add_chunk("static/peer", peer, vec![])
        .run();

    assert_preserved_entry_output(&fixture, "entry\nlater\nready\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/later.js",
        &["later"],
        &[],
    );
}

#[test]
fn cyclic_namespace_lexical_field_read_preserves_the_source_tdz() {
    let source = r#"import * as peer from "./peer.js";
export const value = "ready";
console.log(peer.observed);
"#;
    let peer = r#"import * as app from "./app.js";
export const observed = app.value;
"#;
    let fixture = ProgramPlacement::new(source)
        .with_unassigned_mode(unassigned_mode_inline())
        .place_in_entry(logical_module("lexical_value", &[Member::new("value")]))
        .add_chunk("static/peer", peer, vec![])
        .run();

    assert_preserved_entry_error(&fixture, "Cannot access 'value' before initialization");
}

#[test]
fn anonymous_default_function_is_callable_through_a_split_cycle_before_its_source_line() {
    let source = r#"import run from "./peer.js";
export function invoke() { return run(); }
console.log(invoke());
"#;
    let peer = r#"import { invoke } from "./app.js";
globalThis.answer = invoke();
export default function () { return "hoisted"; }
"#;
    let fixture = ProgramPlacement::new(source)
        .with_unassigned_mode(unassigned_mode_inline())
        .place_in_entry(logical_module("invoke", &[Member::new("invoke")]))
        .add_chunk("static/peer", peer, vec![])
        .run();

    assert_preserved_entry_output(&fixture, "hoisted\n");
}

#[test]
fn anonymous_default_class_cycle_preserves_its_lexical_tdz() {
    let source = r#"import Box from "./peer.js";
export function readDefault() { return Box; }
console.log(readDefault().name);
"#;
    let peer = r#"import { readDefault } from "./app.js";
readDefault();
export default class {}
"#;
    let fixture = ProgramPlacement::new(source)
        .with_unassigned_mode(unassigned_mode_inline())
        .place_in_entry(logical_module(
            "read_default",
            &[Member::new("readDefault")],
        ))
        .add_chunk("static/peer", peer, vec![])
        .run();

    assert_preserved_entry_error(&fixture, "Cannot access 'Box' before initialization");
}

#[test]
fn default_expression_is_a_snapshot_while_the_named_export_stays_live() {
    let source = r#"import initial, { value, replace } from "./peer.js";
const before = initial === value;
const initialLabel = initial.label;
replace();
console.log(before, initial === value, initialLabel, initial.label, value.label);
"#;
    let peer = r#"let value = { label: "original" };
export { value };
export default value;
export function replace() { value = { label: "updated" }; }
"#;
    let fixture = ProgramPlacement::new(source)
        .with_unassigned_mode(unassigned_mode_inline())
        .place_in_entry(logical_module(
            "default_observer",
            &[Member::new("before"), Member::new("initialLabel")],
        ))
        .add_chunk("static/peer", peer, vec![])
        .run();

    assert_preserved_entry_output(&fixture, "true false original original updated\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/default_observer.js",
        &["before", "initialLabel"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/peer/entry.js",
        &["default", "value", "replace"],
        &[],
    );
}
