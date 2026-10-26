//! Export bindings become externally reachable according to their ESM initialization timing.

use debundle_e2e_support::*;

fn split_fixture(source: &'static str) -> FixtureOpts<'static> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values/dynamic",
                &[Member::new("dynamic"), Member::new("prefix")],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

fn assert_split_preserved(source: &'static str) {
    let fixture = run_fixture(split_fixture(source));
    assert_preserved_entry_output(&fixture, "1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/dynamic.js",
        &["dynamic"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/later.js",
        &["later"],
        &[],
    );
}

fn assert_split_rejected(opts: FixtureOpts<'_>) {
    expect_atomic_conflict_rejection(opts, &["values/dynamic", "values/later"], &[]);
}

fn assert_split_rejected_with_peer(
    source: &'static str,
    peer: &'static str,
    admission_overrides: &'static [&'static str],
) {
    let peer_chunks = [("static/peer", peer)];
    assert_split_rejected(
        split_fixture(source)
            .with_extra_chunks(&peer_chunks)
            .with_admission_overrides(admission_overrides),
    );
}

#[test]
fn forward_export_and_late_const_arrow_do_not_reach_an_earlier_unknown_call() {
    assert_split_preserved(
        r#"export { readLater };
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const readLater = () => later;
const later = dynamic;
console.log(later);
"#,
    );
}

#[test]
fn late_exported_class_does_not_reach_an_earlier_unknown_call() {
    assert_split_preserved(
        r#"export { Reader };
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
class Reader { read() { return later; } }
const later = dynamic;
console.log(later);
"#,
    );
}

#[test]
fn exported_hoisted_function_is_reachable_before_its_declaration() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
export function readLater() { return later; }
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
"#,
        r#"import { readLater } from "./app.js";
readLater();
export {};
"#,
        &[],
    );
}

#[test]
fn exported_default_function_is_reachable_before_its_declaration() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
export default function () { return later; }
console.log(later);
"#,
        r#"import readLater from "./app.js";
readLater();
export {};
"#,
        &[],
    );
}

#[test]
fn initialized_exported_const_arrow_reaches_a_later_unknown_call() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
export const readLater = () => later;
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
"#,
        r#"import { readLater } from "./app.js";
readLater();
export {};
"#,
        &[],
    );
}

#[test]
fn reachable_opaque_exported_body_retains_lexical_callback_reads() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
export function opaque() { foreignTag`${readLater}`; }
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
console.log(later);
"#,
        r#"import { opaque } from "./app.js";
opaque();
export {};
"#,
        &[],
    );
}

#[test]
fn reachable_exported_direct_eval_retains_all_local_callables() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
export function evaluate() { eval("readLater()"); }
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
console.log(later);
"#,
        r#"import { evaluate } from "./app.js";
evaluate();
export {};
"#,
        &["a1_eval"],
    );
}

#[test]
fn opaque_export_without_private_capture_does_not_widen_to_all_source_units() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"export function opaque(callback) { callback?.(); }
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
console.log(later);
"#,
        vec![
            logical_module(
                "values/dynamic",
                &[Member::new("dynamic"), Member::new("prefix")],
            ),
            logical_module(
                "values/later",
                &[Member::new("later"), Member::new("readLater")],
            ),
        ],
    ));

    assert_preserved_entry_output(&fixture, "1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/dynamic.js",
        &["dynamic"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/later.js",
        &["later"],
        &[],
    );
}
