//! Unknown-call reachability stops at an unavailable lexical intermediate.

use debundle_e2e_support::*;

#[test]
fn reassigned_superclass_constructor_reads_are_promoted() {
    let source = r#"import { trigger } from './peer.js';
let later = 0;
class Original {}
let Core = Original;
Core = class { constructor() { globalThis.observed = later; } };
class Derived extends Core {}
globalThis.Derived = Derived;
const box = trigger();
later = 42;
console.log(globalThis.observed, later);
export { box, later };
"#;
    let peer = [(
        "static/peer",
        "export function trigger() { new globalThis.Derived(); return {}; }",
    )];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("Original"),
                Member::new("Core"),
                Member::new("Derived"),
                Member::new("box"),
                Member::new("later"),
            ],
            &[
                "Core = class { constructor() { globalThis.observed = later; } };",
                "globalThis.Derived = Derived;",
                "later = 42;",
                "console.log(globalThis.observed, later);",
            ],
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_extra_chunks(&peer);
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module("runtime/original", &[Member::new("Original")]),
            logical_module_with_anon(
                "runtime/core",
                &[Member::new("Core")],
                &["Core = class { constructor() { globalThis.observed = later; } };"],
            ),
            logical_module_with_anon(
                "runtime/derived",
                &[Member::new("Derived")],
                &["globalThis.Derived = Derived;"],
            ),
            logical_module("runtime/call", &[Member::new("box")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    )
    .with_extra_chunks(&peer);
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    expect_atomic_conflict_rejection(split, &["runtime/call", "values/later"], &[]);
}

#[test]
fn superclass_value_is_frozen_when_the_derived_class_is_defined() {
    let source = r#"import { trigger } from './peer.js';
let later = 0;
let Core = class { constructor() { globalThis.observed = later; } };
class Derived extends Core {}
Core = class {};
globalThis.Derived = Derived;
const box = trigger();
later = 42;
console.log(globalThis.observed, later);
export { box, later };
"#;
    let peer = [(
        "static/peer",
        "export function trigger() { new globalThis.Derived(); return {}; }",
    )];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("Core"),
                Member::new("Derived"),
                Member::new("box"),
                Member::new("later"),
            ],
            &[
                "Core = class {};",
                "globalThis.Derived = Derived;",
                "later = 42;",
                "console.log(globalThis.observed, later);",
            ],
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_extra_chunks(&peer);
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "runtime/classes",
                &[Member::new("Core"), Member::new("Derived")],
                &["Core = class {};", "globalThis.Derived = Derived;"],
            ),
            logical_module("runtime/call", &[Member::new("box")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    )
    .with_extra_chunks(&peer);
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    expect_atomic_conflict_rejection(split, &["runtime/call", "values/later"], &[]);
}

#[test]
fn inherited_accessor_reads_remain_visible_through_the_subclass() {
    let source = r#"import { trigger } from './peer.js';
let later = 0;
class Base { get value() { return later; } }
class Derived extends Base {}
globalThis.Derived = Derived;
const box = trigger();
later = 42;
console.log(globalThis.observed, later);
export { box, later };
"#;
    let peer = [(
        "static/peer",
        "export function trigger() { globalThis.observed = new globalThis.Derived().value; return {}; }",
    )];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("Base"),
                Member::new("Derived"),
                Member::new("box"),
                Member::new("later"),
            ],
            &[
                "globalThis.Derived = Derived;",
                "later = 42;",
                "console.log(globalThis.observed, later);",
            ],
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_extra_chunks(&peer);
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module("runtime/base", &[Member::new("Base")]),
            logical_module_with_anon(
                "runtime/derived",
                &[Member::new("Derived")],
                &["globalThis.Derived = Derived;"],
            ),
            logical_module("runtime/call", &[Member::new("box")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    )
    .with_extra_chunks(&peer);
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    expect_atomic_conflict_rejection(split, &["runtime/call", "values/later"], &[]);
}

#[test]
fn unknown_superclass_keeps_the_generic_callback_fallback() {
    let source = r#"import { Base } from './peer.js';
let later = 0;
globalThis.callback = () => later;
class Derived extends Base {}
globalThis.Derived = Derived;
const value = new Derived();
later = 42;
console.log(globalThis.observed, later);
export { value, later };
"#;
    let peer = [(
        "static/peer",
        "export class Base { constructor() { globalThis.observed = globalThis.callback(); } }",
    )];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("Derived"),
                Member::new("value"),
                Member::new("later"),
            ],
            &[
                "globalThis.callback = () => later;",
                "globalThis.Derived = Derived;",
                "later = 42;",
                "console.log(globalThis.observed, later);",
            ],
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_extra_chunks(&peer);
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "runtime/derived",
                &[Member::new("Derived")],
                &[
                    "globalThis.callback = () => later;",
                    "globalThis.Derived = Derived;",
                ],
            ),
            logical_module("runtime/construct", &[Member::new("value")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    )
    .with_extra_chunks(&peer);
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    expect_atomic_conflict_rejection(split, &["runtime/construct", "values/later"], &[]);
}

#[test]
fn unused_constructor_and_getter_are_not_executed_by_subclass_heritage() {
    let source = r#"import { trigger } from './peer.js';
class Calendar { get day() { return later; } }
class Core { constructor() { this.calendar = new Calendar(); } }
class Derived extends Core {}
const box = trigger();
box.flag = 1;
const later = 42;
console.log(box.flag, later);
export { box, later };
"#;
    let peer = [("static/peer", "export function trigger() { return {}; }")];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("Calendar"),
                Member::new("Core"),
                Member::new("Derived"),
                Member::new("box"),
                Member::new("later"),
            ],
            &["box.flag = 1;", "console.log(box.flag, later);"],
        )],
    )
    .with_extra_chunks(&peer)
    .with_unassigned_mode(unassigned_mode_inline());
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "1 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module("runtime/calendar", &[Member::new("Calendar")]),
            logical_module("runtime/core", &[Member::new("Core")]),
            logical_module("runtime/derived", &[Member::new("Derived")]),
            logical_module_with_anon("runtime/call", &[Member::new("box")], &["box.flag = 1;"]),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
    .with_extra_chunks(&peer);
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    let fixture = run_fixture(split);
    assert_preserved_entry_output(&fixture, "1 42\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn inherited_constructor_observes_early_value_when_constructed() {
    let source = r#"let later = 0;
class Core { constructor() { globalThis.observed = later; } }
class Derived extends Core {}
new Derived();
later = 42;
console.log(globalThis.observed, later);
export { later };
"#;
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("Core"),
                Member::new("Derived"),
                Member::new("later"),
            ],
            &[
                "new Derived();",
                "later = 42;",
                "console.log(globalThis.observed, later);",
            ],
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline());
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module("runtime/core", &[Member::new("Core")]),
            logical_module_with_anon(
                "runtime/derived",
                &[Member::new("Derived")],
                &["new Derived();"],
            ),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    expect_atomic_conflict_rejection(split, &["runtime/derived", "values/later"], &[]);
}

#[test]
fn unpublished_pure_class_method_does_not_run_during_opaque_call() {
    let source = r#"import { trigger } from './peer.js';
class Container { read() { return later; } }
const box = trigger();
box.flag = 1;
const later = 42;
console.log(box.flag, later);
export { box, later };
"#;
    let peer = [("static/peer", "export function trigger() { return {}; }")];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("Container"),
                Member::new("box"),
                Member::new("later"),
            ],
            &["box.flag = 1;", "console.log(box.flag, later);"],
        )],
    )
    .with_extra_chunks(&peer)
    .with_unassigned_mode(unassigned_mode_inline());
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "1 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module("runtime/class", &[Member::new("Container")]),
            logical_module_with_anon("runtime/call", &[Member::new("box")], &["box.flag = 1;"]),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
    .with_extra_chunks(&peer);
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    let fixture = run_fixture(split);
    assert_preserved_entry_output(&fixture, "1 42\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn published_class_constructor_and_getter_observe_early_value() {
    let source = r#"import { trigger } from './peer.js';
let later = 0;
class Container {
  constructor() { globalThis.constructed = later; }
  get value() { return later; }
}
globalThis.Container = Container;
const box = trigger();
box.flag = 1;
later = 42;
console.log(globalThis.constructed, globalThis.observed, later);
export { box, later };
"#;
    let peer = [(
        "static/peer",
        "export function trigger() { const instance = new globalThis.Container(); globalThis.observed = instance.value; return {}; }",
    )];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("Container"),
                Member::new("box"),
                Member::new("later"),
            ],
            &[
                "globalThis.Container = Container;",
                "box.flag = 1;",
                "later = 42;",
                "console.log(globalThis.constructed, globalThis.observed, later);",
            ],
        )],
    )
    .with_extra_chunks(&peer)
    .with_unassigned_mode(unassigned_mode_inline());
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "0 0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module("runtime/class", &[Member::new("Container")]),
            logical_module_with_anon(
                "runtime/call",
                &[Member::new("box")],
                &["globalThis.Container = Container;", "box.flag = 1;"],
            ),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    )
    .with_extra_chunks(&peer);
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    expect_atomic_conflict_rejection(split, &["runtime/call", "values/later"], &[]);
}

#[test]
fn post_await_future_command_does_not_make_its_reader_synchronous() {
    let source = r#"import { trigger } from './peer.js';
const reader = () => later;
async function load() { await 0; return command.run(); }
globalThis.saved = load;
const box = trigger();
box.flag = 1;
const command = { run: reader };
const later = 42;
console.log(box.flag, later);
export { box, command, later };
"#;
    let peer = [(
        "static/peer",
        "export function trigger() { globalThis.saved(); return {}; }",
    )];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("reader"),
                Member::new("load"),
                Member::new("box"),
                Member::new("command"),
                Member::new("later"),
            ],
            &[
                "globalThis.saved = load;",
                "box.flag = 1;",
                "console.log(box.flag, later);",
            ],
        )],
    )
    .with_extra_chunks(&peer)
    .with_unassigned_mode(unassigned_mode_inline());
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "1 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module("runtime/functions", &[Member::new("load")]),
            logical_module_with_anon(
                "runtime/call",
                &[Member::new("box")],
                &["globalThis.saved = load;", "box.flag = 1;"],
            ),
            logical_module(
                "values/late",
                &[
                    Member::new("reader"),
                    Member::new("command"),
                    Member::new("later"),
                ],
            ),
        ],
    )
    .with_extra_chunks(&peer);
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    let fixture = run_fixture(split);
    assert_preserved_entry_output(&fixture, "1 42\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn before_await_published_reader_still_observes_early_value() {
    let source = r#"import { trigger } from './peer.js';
let later = 0;
function reader() { return later; }
const command = { run: reader };
async function load() { globalThis.observed = command.run(); await 0; }
globalThis.saved = load;
const box = trigger();
box.flag = 1;
later = 42;
console.log(globalThis.observed, later);
export { box, command, later };
"#;
    let peer = [(
        "static/peer",
        "export function trigger() { globalThis.saved(); return {}; }",
    )];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("reader"),
                Member::new("load"),
                Member::new("box"),
                Member::new("command"),
                Member::new("later"),
            ],
            &[
                "globalThis.saved = load;",
                "box.flag = 1;",
                "later = 42;",
                "console.log(globalThis.observed, later);",
            ],
        )],
    )
    .with_extra_chunks(&peer)
    .with_unassigned_mode(unassigned_mode_inline());
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module(
                "runtime/functions",
                &[
                    Member::new("reader"),
                    Member::new("command"),
                    Member::new("load"),
                ],
            ),
            logical_module_with_anon(
                "runtime/call",
                &[Member::new("box")],
                &["globalThis.saved = load;", "box.flag = 1;"],
            ),
            logical_module_with_anon("values/late", &[Member::new("later")], &["later = 42;"]),
        ],
    )
    .with_extra_chunks(&peer);
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    expect_atomic_conflict_rejection(split, &["runtime/call", "values/late"], &[]);
}

const PEER: &str = r#"import { readLater } from "./provider.js";
globalThis.runBeforeInitialization = function runBeforeInitialization() {
  try {
    globalThis.earlyValue = readLater();
    globalThis.earlyRead = "returned";
  } catch {
    globalThis.earlyRead = "threw";
  }
  return "prefix";
};
export const ready = true;
"#;

const PEER_WITH_CALLBACK: &str = r#"import { readLater } from "./provider.js";
globalThis.runBeforeInitialization = function runBeforeInitialization(callback) {
  try {
    globalThis.earlyValue = (callback || readLater)();
    globalThis.earlyRead = "returned";
  } catch {
    globalThis.earlyRead = "threw";
  }
  return "prefix";
};
export const ready = true;
"#;

const REGISTRY: &str = r#"export const registry = {
  registerProvider(provider) {
    globalThis.savedProvider = provider;
  },
};
"#;

fn split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &[
                Member::new("prefix"),
                Member::new("dynamic"),
                Member::new("getLater"),
                Member::new("readLater"),
            ],
        ),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

fn class_split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &[
                Member::new("prefix"),
                Member::new("dynamic"),
                Member::new("Reader"),
            ],
        ),
        logical_module("values/later", &[Member::new("later")]),
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

fn registered_provider_modules(with_hint: bool) -> Vec<LogicalModuleEntry> {
    let registry = if with_hint {
        Member::new("registry").with_no_sync_callback_members(&["registerProvider"])
    } else {
        Member::new("registry")
    };
    vec![
        logical_module("values/provider", &[Member::new("provider")]),
        logical_module("values/later", &[Member::new("later")]),
        logical_module("api/registry", &[registry]),
    ]
}

fn independent_value_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &[Member::new("prefix"), Member::new("dynamic")],
        ),
        logical_module(
            "values/value",
            &[Member::new("value"), Member::new("readLater")],
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

fn independent_value_colocated_fixture(source: &'static str) -> Fixture {
    let peer_chunks = [("static/peer", PEER)];
    run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values/all",
                &[
                    Member::new("prefix"),
                    Member::new("dynamic"),
                    Member::new("value"),
                    Member::new("readLater"),
                ],
                &["value = 42;"],
            )],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&peer_chunks),
    )
}

fn independent_later_colocated_fixture(source: &'static str) -> Fixture {
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
                    Member::new("readLater"),
                ],
            )],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&peer_chunks),
    )
}

fn expect_independent_later_conflict(source: &'static str, peer: &'static str) {
    let peer_chunks = [("static/peer", peer)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, independent_later_modules())
            .with_chunk_id("static/provider")
            .with_extra_chunks(&peer_chunks),
        &["values/dynamic", "values/later"],
        &[],
    );
}

fn expect_independent_later_alias_conflict(source: &'static str) {
    let peer_chunks = [("static/peer", PEER_WITH_CALLBACK)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, independent_later_modules())
            .with_chunk_id("static/provider")
            .with_extra_chunks(&peer_chunks),
        &["values/dynamic", "values/later"],
        &[],
    );
}

fn expect_independent_value_conflict(source: &'static str) {
    let peer_chunks = [("static/peer", PEER)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, independent_value_modules())
            .with_chunk_id("static/provider")
            .with_extra_chunks(&peer_chunks),
        &["values/value"],
        &["eager_rebind"],
    );
}

fn registered_provider_fixture(source: &'static str, with_hint: bool) -> Fixture {
    let registry_chunks = [("static/registry", REGISTRY)];
    run_fixture(
        FixtureOpts::new(source, registered_provider_modules(with_hint))
            .with_chunk_id("static/provider")
            .with_extra_chunks(&registry_chunks),
    )
}

fn expect_independent_later_colocated_runtime(source: &'static str, expected: &str) {
    let fixture = independent_later_colocated_fixture(source);
    assert_preserved_entry_output(&fixture, expected);
    assert_generated_module_after_entry_script(
        &fixture,
        "const values = await import('./static/provider/modules/values/all.js');\n\
         console.log(values.readLater());\n",
        "42\n",
    );
}

fn expect_alias_colocated_runtime(source: &'static str, expected: &str) {
    let peer_chunks = [("static/peer", PEER_WITH_CALLBACK)];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "values/all",
                &[
                    Member::new("prefix"),
                    Member::new("dynamic"),
                    Member::new("later"),
                    Member::new("readLater"),
                    Member::new("capturedReader"),
                ],
            )],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&peer_chunks),
    );
    assert_preserved_entry_output(&fixture, expected);
    assert_generated_module_after_entry_script(
        &fixture,
        "const values = await import('./static/provider/modules/values/all.js');\n\
         console.log(values.readLater());\n",
        "42\n",
    );
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
        r#"import { ready } from "./peer.js";
export function readLater() { return getLater(); }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const getLater = () => dynamic;
const later = 42;
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
    );

    assert_preserved_entry_output(&fixture, "threw true\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const provider = await import('./static/provider/entry.js');\n\
         const dynamic = await import('./static/provider/modules/values/dynamic.js');\n\
         console.log(provider.readLater() === dynamic.dynamic);\n",
        "true\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn split_modules_preserve_the_early_tdz_and_later_read() {
    let fixture = split_fixture(
        r#"import { ready } from "./peer.js";
export function readLater() { return getLater(); }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = { ready: "ready" };
const getLater = () => dynamic;
const later = 42;
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
    );

    assert_preserved_entry_output(&fixture, "threw true\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const provider = await import('./static/provider/entry.js');\n\
         const dynamic = await import('./static/provider/modules/values/dynamic.js');\n\
         console.log(provider.readLater() === dynamic.dynamic);\n",
        "true\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn colocated_late_const_callable_runs_after_module_initialization() {
    let fixture = colocated_fixture(
        r#"import { ready } from "./peer.js";
export function readLater() { return getLater(); }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const later = dynamic;
const getLater = () => later;
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
    );

    assert_preserved_entry_output(&fixture, "threw true\n");
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
        r#"import { ready } from "./peer.js";
export function readLater() { return new Reader().read(); }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
class Reader { read() { return dynamic; } }
const later = 42;
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
    );

    assert_preserved_entry_output(&fixture, "threw true\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const provider = await import('./static/provider/entry.js');\n\
         const dynamic = await import('./static/provider/modules/values/dynamic.js');\n\
         console.log(provider.readLater() === dynamic.dynamic);\n",
        "true\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn initialized_class_really_reaches_the_later_binding() {
    expect_class_split_conflict(
        r#"import { ready } from "./peer.js";
export function readLater() { return new Reader().read(); }
class Reader { read() { return later; } }
const prefix = ready && globalThis.runBeforeInitialization();
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
fn split_rejects_const_tdz_observed_during_unknown_call_without_iife_writes() {
    expect_independent_later_conflict(
        r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
const later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
        PEER,
    );
}

#[test]
fn split_rejects_const_tdz_observed_during_unknown_call_with_fresh_iife_writes() {
    expect_independent_later_conflict(
        r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
        PEER,
    );
}

#[test]
fn colocated_unknown_call_preserves_const_tdz_before_later_read() {
    expect_independent_later_colocated_runtime(
        r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
        "threw undefined 42\n",
    );
}

#[test]
fn split_rejects_var_read_before_its_assignment() {
    expect_independent_later_conflict(
        r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
var later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
        PEER,
    );
    expect_independent_later_colocated_runtime(
        r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
var later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
        "returned undefined 42\n",
    );
}

#[test]
fn split_accepts_var_initialized_before_unknown_call() {
    let fixture = independent_later_fixture(
        r#"import { ready } from "./peer.js";
var later = 42;
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
    );

    assert_preserved_entry_output(&fixture, "returned 42 42\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const later = await import('./static/provider/modules/values/later.js');\n\
         console.log(later.readLater());\n",
        "42\n",
    );
}

#[test]
fn split_accepts_const_available_before_unknown_call() {
    let fixture = independent_later_fixture(
        r#"import { ready } from "./peer.js";
const later = 42;
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
    );

    assert_preserved_entry_output(&fixture, "returned 42 42\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const later = await import('./static/provider/modules/values/later.js');\n\
         console.log(later.readLater());\n",
        "42\n",
    );
}

#[test]
fn split_is_rejected_when_callback_value_precedes_a_later_var_reassignment() {
    let source = r#"import { ready } from "./peer.js";
var value = 1;
export function readLater() { return value; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
value = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, value);
"#;
    expect_independent_value_conflict(source);

    let fixture = independent_value_colocated_fixture(source);
    assert_preserved_entry_output(&fixture, "returned 1 42\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const values = await import('./static/provider/modules/values/all.js');\n\
         console.log(values.readLater());\n",
        "42\n",
    );
}

#[test]
fn split_rejects_previously_captured_callback_reading_const_tdz() {
    expect_independent_later_alias_conflict(
        r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const capturedReader = readLater;
const prefix = ready && globalThis.runBeforeInitialization(capturedReader);
const dynamic = {};
const later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
    );
    expect_alias_colocated_runtime(
        r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const capturedReader = readLater;
const prefix = ready && globalThis.runBeforeInitialization(capturedReader);
const dynamic = {};
const later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
        "threw undefined 42\n",
    );
}

#[test]
fn no_sync_callback_member_hint_keeps_stored_provider_deferred() {
    let source = r#"import { registry as importedRegistry } from "./registry.js";
const registry = importedRegistry;
const provider = { read() { return later; } };
registry.registerProvider(provider);
const later = 42;
console.log("ready");
"#;
    let fixture = registered_provider_fixture(source, true);

    assert_preserved_entry_output(&fixture, "ready\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "console.log(globalThis.savedProvider.read());\n",
        "42\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn unhinted_unknown_registration_still_preserves_callback_tdz() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"import { registry as importedRegistry } from "./registry.js";
const registry = importedRegistry;
const provider = { read() { return later; } };
registry.registerProvider(provider);
const later = 42;
console.log("ready");
"#,
            registered_provider_modules(false),
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&[("static/registry", REGISTRY)]),
        &["values/later"],
        &["sequenced"],
    );
}
