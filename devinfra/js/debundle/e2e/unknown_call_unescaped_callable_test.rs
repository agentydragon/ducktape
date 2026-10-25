//! An opaque call cannot invoke a module-local callable that has not escaped.

use debundle_e2e_support::*;

#[test]
fn unpublished_imported_callback_factory_result_stays_deferred() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { observe } from './peer.js';
const prefix = new globalThis.Error();
const wrapped = observe(() => later);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
console.log(wrapped());
"#,
            vec![
                logical_module(
                    "values/dynamic",
                    &[Member::new("dynamic"), Member::new("prefix")],
                ),
                logical_module(
                    "values/later",
                    &[Member::new("later"), Member::new("wrapped")],
                ),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function observe(callback) { return (...args) => callback(...args); }\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["observe"])
            .build()]),
    );

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/dynamic.js",
        &["dynamic"],
        &["later"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/later.js",
        &["later", "wrapped"],
        &["dynamic"],
    );
    assert_preserved_entry_output(&fixture, "1\n1\n");
}

#[test]
fn published_imported_callback_factory_result_keeps_later_read() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"import { observe } from './peer.js';
const prefix = new globalThis.Error();
const wrapped = observe(() => later);
globalThis.saved = wrapped;
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
"#,
            vec![
                logical_module(
                    "values/dynamic",
                    &[Member::new("dynamic"), Member::new("prefix")],
                ),
                logical_module(
                    "values/later",
                    &[Member::new("later"), Member::new("wrapped")],
                ),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function observe(callback) { return (...args) => callback(...args); }\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["observe"])
            .build()]),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn earlier_unknown_call_does_not_promote_unescaped_local_reader() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const prefix = new globalThis.Error();
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

#[test]
fn global_or_empty_object_fallback_does_not_expose_local_callables() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const prefix = (() => {
const runtime = typeof window !== "undefined" ? window : globalThis || {};
const entry = new runtime.Error();
runtime.registry = runtime.registry || {};
runtime.registry[entry.name] = entry.message;
return undefined;
})();
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

#[test]
fn callback_escaped_after_unknown_call_remains_a_possible_accessor() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"new globalThis.Error();
globalThis.callback = readLater;
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { later; }
"#,
            vec![
                logical_module("values/dynamic", &[Member::new("dynamic")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn callback_at_active_call_site_remains_reachable_after_nested_unknown_call() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"const target = { set x(value) { value(); } };
function exposeArgument() {
  consume(arguments);
  target.x = arguments[0];
}
const dynamic = (exposeArgument(readLater), 1);
var later = dynamic;
function readLater() { return later; }
"#,
            vec![
                logical_module("values/dynamic", &[Member::new("dynamic")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn implicit_arguments_object_keeps_passed_local_callables_reachable() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"function exposeArgument(callback) { consume(arguments); }
exposeArgument(readLater);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
"#,
            vec![
                logical_module("values/dynamic", &[Member::new("dynamic")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn cyclic_importer_keeps_exported_hoisted_callable_reachable_before_evaluation() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"import "./peer.js";
new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { later; }
export { dynamic, later, readLater };
"#,
            vec![
                logical_module("values/dynamic", &[Member::new("dynamic")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            r#"import { readLater } from "./app.js";
readLater();
"#,
        )]),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn direct_eval_keeps_local_callable_fallback() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"eval("readLater()");
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
var later = dynamic;
function readLater() { later; }
"#,
            vec![
                logical_module("values/dynamic", &[Member::new("dynamic")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_admission_overrides(&["a1_eval"]),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn unsupported_top_level_escape_keeps_later_local_callables_reachable() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"foreignTag`${readLater}`;
new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
"#,
            vec![
                logical_module("values/dynamic", &[Member::new("dynamic")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn unsupported_callable_body_keeps_later_local_callables_reachable() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"function opaqueAccessor() { foreignTag`${readLater}`; }
opaqueAccessor();
new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
"#,
            vec![
                logical_module("values/dynamic", &[Member::new("dynamic")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn passing_this_to_unknown_code_keeps_local_object_reachable() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"const receiver = {
  inspectReceiver() { consume(this); },
  readLater,
};
receiver.inspectReceiver();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
"#,
            vec![
                logical_module("values/dynamic", &[Member::new("dynamic")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn opaque_prototype_setter_still_reaches_escaped_local_callback() {
    let source = r#"const earlier = "later";
let observed = "";
function readLater() { return earlier + String(later); }
Object.defineProperty(Object.prototype, "__debundle_test_probe__", {
  set(value) { observed = readLater(); },
});
const dynamic = ((object) => {
  object[(object.key = "__debundle_test_probe__")] = "value";
  return observed;
})({});
var later = dynamic;
console.log(dynamic);
"#;

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/dynamic", &[Member::new("dynamic")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );

    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values/all",
            &[
                Member::new("dynamic"),
                Member::new("later"),
                Member::new("readLater"),
                Member::new("observed"),
            ],
            &[
                r#"Object.defineProperty(Object.prototype, "__debundle_test_probe__", {
  set(value) { observed = readLater(); },
});"#,
                "console.log(dynamic);",
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "laterundefined\n");
}
