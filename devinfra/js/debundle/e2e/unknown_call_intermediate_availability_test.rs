//! Unknown-call reachability stops at an unavailable lexical intermediate.

use debundle_e2e_support::*;

const PEER: &str = r#"import { readLater } from "./provider.js";
globalThis.runBeforeInitialization = function runBeforeInitialization() {
  try {
    readLater();
    globalThis.earlyRead = "returned";
  } catch {
    globalThis.earlyRead = "threw";
  }
  return "prefix";
};
export {};
"#;

fn split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &[Member::new("prefix"), Member::new("dynamic")],
        ),
        logical_module(
            "values/later",
            &[
                Member::new("later"),
                Member::new("getLater"),
                Member::new("readLater"),
            ],
        ),
    ]
}

fn class_split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &[Member::new("prefix"), Member::new("dynamic")],
        ),
        logical_module(
            "values/later",
            &[
                Member::new("later"),
                Member::new("Reader"),
                Member::new("readLater"),
            ],
        ),
    ]
}

fn independent_later_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &[Member::new("prefix"), Member::new("dynamic")],
        ),
        logical_module(
            "values/later",
            &[Member::new("later"), Member::new("readLater")],
        ),
    ]
}

fn split_fixture(source: &'static str) -> Fixture {
    let peer_chunks = [("static/peer", PEER)];
    run_fixture(
        FixtureOpts::new(source, split_modules())
            .with_chunk_id("static/provider")
            .with_extra_chunks(&peer_chunks),
    )
}

fn colocated_fixture(source: &'static str) -> Fixture {
    let peer_chunks = [("static/peer", PEER)];
    run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "values/all",
                &[
                    Member::new("prefix"),
                    Member::new("dynamic"),
                    Member::new("later"),
                    Member::new("getLater"),
                    Member::new("readLater"),
                ],
            )],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&peer_chunks),
    )
}

fn class_split_fixture(source: &'static str) -> Fixture {
    let peer_chunks = [("static/peer", PEER)];
    run_fixture(
        FixtureOpts::new(source, class_split_modules())
            .with_chunk_id("static/provider")
            .with_extra_chunks(&peer_chunks),
    )
}

fn independent_later_fixture(source: &'static str) -> Fixture {
    let peer_chunks = [("static/peer", PEER)];
    run_fixture(
        FixtureOpts::new(source, independent_later_modules())
            .with_chunk_id("static/provider")
            .with_extra_chunks(&peer_chunks),
    )
}

fn expect_class_split_conflict(source: &'static str) {
    let peer_chunks = [("static/peer", PEER)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, class_split_modules())
            .with_chunk_id("static/provider")
            .with_extra_chunks(&peer_chunks),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn late_const_callable_is_unavailable_during_unknown_call_reentry() {
    let fixture = split_fixture(
        r#"import "./peer.js";
export function readLater() { return getLater(); }
const prefix = globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const later = dynamic;
const getLater = () => later;
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
    );

    assert_entry_output(&fixture, "threw true\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const dynamic = await import('./static/provider/modules/values/dynamic.js');\n\
         const later = await import('./static/provider/modules/values/later.js');\n\
         console.log(later.readLater() === dynamic.dynamic);\n",
        "true\n",
    );
    assert_module_exports(
        &fixture.out_root,
        "static/provider/modules/values/dynamic.js",
        &["dynamic"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/provider/modules/values/later.js",
        &["later", "readLater"],
        &[],
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn split_modules_preserve_the_early_tdz_and_later_read() {
    let fixture = split_fixture(
        r#"import "./peer.js";
export function readLater() { return getLater(); }
const prefix = globalThis.runBeforeInitialization();
const dynamic = { ready: "ready" };
const later = dynamic;
const getLater = () => later;
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
    );

    assert_entry_output(&fixture, "threw true\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const dynamic = await import('./static/provider/modules/values/dynamic.js');\n\
         const later = await import('./static/provider/modules/values/later.js');\n\
         console.log(later.readLater() === dynamic.dynamic);\n",
        "true\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn colocated_late_const_callable_runs_after_module_initialization() {
    let fixture = colocated_fixture(
        r#"import "./peer.js";
export function readLater() { return getLater(); }
const prefix = globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const later = dynamic;
const getLater = () => later;
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
    );

    assert_entry_output(&fixture, "threw true\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const values = await import('./static/provider/modules/values/all.js');\n\
         console.log(values.readLater() === values.dynamic);\n",
        "true\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn late_class_intermediate_is_unavailable_during_unknown_call_reentry() {
    let fixture = class_split_fixture(
        r#"import "./peer.js";
export function readLater() { return new Reader().read(); }
const prefix = globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const later = dynamic;
class Reader { read() { return later; } }
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
    );

    assert_entry_output(&fixture, "threw true\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const dynamic = await import('./static/provider/modules/values/dynamic.js');\n\
         const later = await import('./static/provider/modules/values/later.js');\n\
         console.log(later.readLater() === dynamic.dynamic);\n",
        "true\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn initialized_class_really_reaches_the_later_binding() {
    expect_class_split_conflict(
        r#"import "./peer.js";
export function readLater() { return new Reader().read(); }
class Reader { read() { return later; } }
const prefix = globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const later = dynamic;
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
    );
}

#[test]
fn unknown_call_keeps_callsite_order_without_iife_property_writes() {
    let fixture = independent_later_fixture(
        r#"import "./peer.js";
export function readLater() { return later; }
const prefix = globalThis.runBeforeInitialization();
const dynamic = {};
const later = 42;
console.log(globalThis.earlyRead, later);
"#,
    );

    assert_preserved_entry_output(&fixture, "threw 42\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const later = await import('./static/provider/modules/values/later.js');\n\
         console.log(later.readLater());\n",
        "42\n",
    );
}

#[test]
fn unknown_call_keeps_callsite_order_with_fresh_iife_property_writes() {
    let fixture = independent_later_fixture(
        r#"import "./peer.js";
export function readLater() { return later; }
const prefix = globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const later = 42;
console.log(globalThis.earlyRead, later);
"#,
    );

    assert_preserved_entry_output(&fixture, "threw 42\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const later = await import('./static/provider/modules/values/later.js');\n\
         console.log(later.readLater());\n",
        "42\n",
    );
}
