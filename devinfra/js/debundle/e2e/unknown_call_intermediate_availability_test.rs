//! Unknown-call reachability stops at an unavailable lexical intermediate.

use debundle_e2e_support::*;

#[test]
fn heritage_read_proof_does_not_cover_other_class_evaluation() {
    for (heritage, body) in [
        ("(globalThis.observed = later, Core)", ""),
        ("holder.Base", ""),
        ("Core", "static snapshot = (globalThis.observed = later);"),
        ("Core", "[(globalThis.observed = later, 'read')]() {}"),
        ("(Core.read(), Core)", ""),
        ("Core", "static snapshot = Core.read();"),
        ("Core", "[Core.read()]() {}"),
    ] {
        let source = format!(
            "let later = 0;\n\
             class Core {{ static read() {{ globalThis.observed = later; return 'read'; }} }}\n\
             const holder = {{ get Base() {{ globalThis.observed = later; return Core; }} }};\n\
             class Derived extends {heritage} {{ {body} }}\n\
             later = 42;\n\
             console.log(globalThis.observed, later);\n"
        );
        let mut colocated = FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "all",
                &binding_members(["later", "Core", "holder", "Derived"]),
                &["later = 42;"],
            )],
        );
        colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

        let mut split = FixtureOpts::new(
            &source,
            vec![
                logical_module("classes", &binding_members(["Core", "holder", "Derived"])),
                logical_module_with_anon("later", &[Member::new("later")], &["later = 42;"]),
            ],
        );
        split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        expect_atomic_conflict_rejection(split, &["classes", "later"], &[]);
    }
}

#[test]
fn lexical_heritage_does_not_cover_a_throwing_static_initializer() {
    let fixture = run_fixture(FixtureOpts::new(
        "class Core {}\nclass Derived extends Core { static value = (() => { throw new Error('static evaluation'); })(); }\n",
        vec![logical_module(
            "classes",
            &binding_members(["Core", "Derived"]),
        )],
    ));
    assert_preserved_entry_error(&fixture, "Error: static evaluation");
}

enum SplitOutcome {
    /// The split is rejected, naming these logical modules as atomic.
    Conflict(&'static [&'static str]),
    /// The split is accepted and prints this entry output.
    Accepted(&'static str),
}

/// One program run colocated (`runtime/all`, inline unassigned mode) and then
/// split; both runs disable the synchronous module loader.
struct ClassSplitCase {
    label: &'static str,
    source: &'static str,
    peer: Option<&'static str>,
    colocated: LogicalModuleEntry,
    colocated_output: &'static str,
    split: Vec<LogicalModuleEntry>,
    outcome: SplitOutcome,
}

impl ClassSplitCase {
    fn check(self) {
        println!("case: {}", self.label);
        let peer: Vec<(&str, &str)> = self
            .peer
            .map(|peer| ("static/peer", peer))
            .into_iter()
            .collect();
        let mut colocated = FixtureOpts::new(self.source, vec![self.colocated])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_extra_chunks(&peer);
        colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        assert_preserved_entry_output(&run_fixture(colocated), self.colocated_output);

        let mut split = FixtureOpts::new(self.source, self.split).with_extra_chunks(&peer);
        split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        match self.outcome {
            SplitOutcome::Conflict(modules) => {
                expect_atomic_conflict_rejection(split, modules, &[])
            }
            SplitOutcome::Accepted(output) => {
                let fixture = run_fixture(split);
                assert_preserved_entry_output(&fixture, output);
                assert_all_emitted_js_checks(&fixture);
            }
        }
    }
}

#[test]
fn class_and_published_reader_runs_observe_the_early_value() {
    for case in [
        ClassSplitCase {
            label: "superclass static initializer",
            source: r#"let later = 0;
class Core { static snapshot = later; }
class Derived extends Core {}
later = 42;
console.log(Derived.snapshot, later);
export { Derived, later };
"#,
            peer: None,
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["Core", "Derived", "later"]),
                &["later = 42;", "console.log(Derived.snapshot, later);"],
            ),
            colocated_output: "0 42\n",
            split: vec![
                logical_module("runtime/core", &[Member::new("Core")]),
                logical_module("runtime/derived", &[Member::new("Derived")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
            outcome: SplitOutcome::Conflict(&["runtime/core", "values/later"]),
        },
        ClassSplitCase {
            // Reassigned superclass constructor reads are promoted.
            label: "reassigned superclass constructor",
            source: r#"import { trigger } from './peer.js';
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
"#,
            peer: Some("export function trigger() { new globalThis.Derived(); return {}; }"),
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["Original", "Core", "Derived", "box", "later"]),
                &[
                    "Core = class { constructor() { globalThis.observed = later; } };",
                    "globalThis.Derived = Derived;",
                    "later = 42;",
                    "console.log(globalThis.observed, later);",
                ],
            ),
            colocated_output: "0 42\n",
            split: vec![
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
            outcome: SplitOutcome::Conflict(&["runtime/call", "values/later"]),
        },
        ClassSplitCase {
            // The superclass value is frozen when the derived class is defined.
            label: "superclass rebound after definition",
            source: r#"import { trigger } from './peer.js';
let later = 0;
let Core = class { constructor() { globalThis.observed = later; } };
class Derived extends Core {}
Core = class {};
globalThis.Derived = Derived;
const box = trigger();
later = 42;
console.log(globalThis.observed, later);
export { box, later };
"#,
            peer: Some("export function trigger() { new globalThis.Derived(); return {}; }"),
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["Core", "Derived", "box", "later"]),
                &[
                    "Core = class {};",
                    "globalThis.Derived = Derived;",
                    "later = 42;",
                    "console.log(globalThis.observed, later);",
                ],
            ),
            colocated_output: "0 42\n",
            split: vec![
                logical_module_with_anon(
                    "runtime/classes",
                    &binding_members(["Core", "Derived"]),
                    &["Core = class {};", "globalThis.Derived = Derived;"],
                ),
                logical_module("runtime/call", &[Member::new("box")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
            outcome: SplitOutcome::Conflict(&["runtime/call", "values/later"]),
        },
        ClassSplitCase {
            // Inherited accessor reads remain visible through the subclass.
            label: "inherited accessor",
            source: r#"import { trigger } from './peer.js';
let later = 0;
class Base { get value() { return later; } }
class Derived extends Base {}
globalThis.Derived = Derived;
const box = trigger();
later = 42;
console.log(globalThis.observed, later);
export { box, later };
"#,
            peer: Some(
                "export function trigger() { globalThis.observed = new globalThis.Derived().value; return {}; }",
            ),
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["Base", "Derived", "box", "later"]),
                &[
                    "globalThis.Derived = Derived;",
                    "later = 42;",
                    "console.log(globalThis.observed, later);",
                ],
            ),
            colocated_output: "0 42\n",
            split: vec![
                logical_module("runtime/base", &[Member::new("Base")]),
                logical_module_with_anon(
                    "runtime/derived",
                    &[Member::new("Derived")],
                    &["globalThis.Derived = Derived;"],
                ),
                logical_module("runtime/call", &[Member::new("box")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
            outcome: SplitOutcome::Conflict(&["runtime/call", "values/later"]),
        },
        ClassSplitCase {
            // An unknown superclass keeps the generic callback fallback.
            label: "unknown superclass",
            source: r#"import { Base } from './peer.js';
let later = 0;
globalThis.callback = () => later;
class Derived extends Base {}
globalThis.Derived = Derived;
const value = new Derived();
later = 42;
console.log(globalThis.observed, later);
export { value, later };
"#,
            peer: Some(
                "export class Base { constructor() { globalThis.observed = globalThis.callback(); } }",
            ),
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["Derived", "value", "later"]),
                &[
                    "globalThis.callback = () => later;",
                    "globalThis.Derived = Derived;",
                    "later = 42;",
                    "console.log(globalThis.observed, later);",
                ],
            ),
            colocated_output: "0 42\n",
            split: vec![
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
            outcome: SplitOutcome::Conflict(&["runtime/construct", "values/later"]),
        },
        ClassSplitCase {
            label: "inherited constructor when constructed",
            source: r#"let later = 0;
class Core { constructor() { globalThis.observed = later; } }
class Derived extends Core {}
new Derived();
later = 42;
console.log(globalThis.observed, later);
export { later };
"#,
            peer: None,
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["Core", "Derived", "later"]),
                &[
                    "new Derived();",
                    "later = 42;",
                    "console.log(globalThis.observed, later);",
                ],
            ),
            colocated_output: "0 42\n",
            split: vec![
                logical_module("runtime/core", &[Member::new("Core")]),
                logical_module_with_anon(
                    "runtime/derived",
                    &[Member::new("Derived")],
                    &["new Derived();"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
            outcome: SplitOutcome::Conflict(&["runtime/derived", "values/later"]),
        },
        ClassSplitCase {
            label: "published class constructor and getter",
            source: r#"import { trigger } from './peer.js';
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
"#,
            peer: Some(
                "export function trigger() { const instance = new globalThis.Container(); globalThis.observed = instance.value; return {}; }",
            ),
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["Container", "box", "later"]),
                &[
                    "globalThis.Container = Container;",
                    "box.flag = 1;",
                    "later = 42;",
                    "console.log(globalThis.constructed, globalThis.observed, later);",
                ],
            ),
            colocated_output: "0 0 42\n",
            split: vec![
                logical_module("runtime/class", &[Member::new("Container")]),
                logical_module_with_anon(
                    "runtime/call",
                    &[Member::new("box")],
                    &["globalThis.Container = Container;", "box.flag = 1;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
            outcome: SplitOutcome::Conflict(&["runtime/call", "values/later"]),
        },
        ClassSplitCase {
            // A published reader called before the first `await` still runs
            // synchronously inside the unknown call.
            label: "before-await published reader",
            source: r#"import { trigger } from './peer.js';
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
"#,
            peer: Some("export function trigger() { globalThis.saved(); return {}; }"),
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["reader", "load", "box", "command", "later"]),
                &[
                    "globalThis.saved = load;",
                    "box.flag = 1;",
                    "later = 42;",
                    "console.log(globalThis.observed, later);",
                ],
            ),
            colocated_output: "0 42\n",
            split: vec![
                logical_module(
                    "runtime/functions",
                    &binding_members(["reader", "command", "load"]),
                ),
                logical_module_with_anon(
                    "runtime/call",
                    &[Member::new("box")],
                    &["globalThis.saved = load;", "box.flag = 1;"],
                ),
                logical_module_with_anon("values/late", &[Member::new("later")], &["later = 42;"]),
            ],
            outcome: SplitOutcome::Conflict(&["runtime/call", "values/late"]),
        },
    ] {
        case.check();
    }
}

#[test]
fn unreached_class_members_and_post_await_readers_allow_the_split() {
    for case in [
        ClassSplitCase {
            // Subclass heritage does not execute an unused constructor or getter.
            label: "unused constructor and getter",
            source: r#"import { trigger } from './peer.js';
class Calendar { get day() { return later; } }
class Core { constructor() { this.calendar = new Calendar(); } }
class Derived extends Core {}
const box = trigger();
box.flag = 1;
const later = 42;
console.log(box.flag, later);
export { box, later };
"#,
            peer: Some("export function trigger() { return {}; }"),
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["Calendar", "Core", "Derived", "box", "later"]),
                &["box.flag = 1;", "console.log(box.flag, later);"],
            ),
            colocated_output: "1 42\n",
            split: vec![
                logical_module("runtime/calendar", &[Member::new("Calendar")]),
                logical_module("runtime/core", &[Member::new("Core")]),
                logical_module("runtime/derived", &[Member::new("Derived")]),
                logical_module_with_anon("runtime/call", &[Member::new("box")], &["box.flag = 1;"]),
                logical_module("values/later", &[Member::new("later")]),
            ],
            outcome: SplitOutcome::Accepted("1 42\n"),
        },
        ClassSplitCase {
            // An unpublished pure class method does not run during an opaque call.
            label: "unpublished pure class method",
            source: r#"import { trigger } from './peer.js';
class Container { read() { return later; } }
const box = trigger();
box.flag = 1;
const later = 42;
console.log(box.flag, later);
export { box, later };
"#,
            peer: Some("export function trigger() { return {}; }"),
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["Container", "box", "later"]),
                &["box.flag = 1;", "console.log(box.flag, later);"],
            ),
            colocated_output: "1 42\n",
            split: vec![
                logical_module("runtime/class", &[Member::new("Container")]),
                logical_module_with_anon("runtime/call", &[Member::new("box")], &["box.flag = 1;"]),
                logical_module("values/later", &[Member::new("later")]),
            ],
            outcome: SplitOutcome::Accepted("1 42\n"),
        },
        ClassSplitCase {
            // A post-await future command does not make its reader synchronous.
            label: "post-await future command",
            source: r#"import { trigger } from './peer.js';
const reader = () => later;
async function load() { await 0; return command.run(); }
globalThis.saved = load;
const box = trigger();
box.flag = 1;
const command = { run: reader };
const later = 42;
console.log(box.flag, later);
export { box, command, later };
"#,
            peer: Some("export function trigger() { globalThis.saved(); return {}; }"),
            colocated: logical_module_with_anon(
                "runtime/all",
                &binding_members(["reader", "load", "box", "command", "later"]),
                &[
                    "globalThis.saved = load;",
                    "box.flag = 1;",
                    "console.log(box.flag, later);",
                ],
            ),
            colocated_output: "1 42\n",
            split: vec![
                logical_module("runtime/functions", &[Member::new("load")]),
                logical_module_with_anon(
                    "runtime/call",
                    &[Member::new("box")],
                    &["globalThis.saved = load;", "box.flag = 1;"],
                ),
                logical_module(
                    "values/late",
                    &binding_members(["reader", "command", "later"]),
                ),
            ],
            outcome: SplitOutcome::Accepted("1 42\n"),
        },
    ] {
        case.check();
    }
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
            &binding_members(["prefix", "dynamic", "getLater", "readLater"]),
        ),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

fn class_split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &binding_members(["prefix", "dynamic", "Reader"]),
        ),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

fn independent_later_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("values/dynamic", &binding_members(["prefix", "dynamic"])),
        logical_module("values/later", &binding_members(["later", "readLater"])),
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
        logical_module("values/dynamic", &binding_members(["prefix", "dynamic"])),
        logical_module("values/value", &binding_members(["value", "readLater"])),
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
                &binding_members(["prefix", "dynamic", "later", "getLater", "readLater"]),
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
            .with_import_order(&["values/dynamic", "values/later", "residual"])
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
                &binding_members(["prefix", "dynamic", "value", "readLater"]),
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
                &binding_members(["prefix", "dynamic", "later", "readLater"]),
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
            .with_import_order(&["values/dynamic", "values/later", "residual"])
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

fn registered_provider_opts(with_hint: bool) -> FixtureOpts<'static> {
    let source = r#"import { registry as importedRegistry } from "./registry.js";
const registry = importedRegistry;
const provider = { read() { return later; } };
registry.registerProvider(provider);
const later = 42;
console.log("ready");
"#;
    FixtureOpts::new(source, registered_provider_modules(with_hint))
        .with_chunk_id("static/provider")
        .with_extra_chunks(&[("static/registry", REGISTRY)])
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
                &binding_members(["prefix", "dynamic", "later", "readLater", "capturedReader"]),
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
            .with_import_order(&["values/dynamic", "values/later", "residual"])
            .with_extra_chunks(&peer_chunks),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn late_intermediates_are_unavailable_during_unknown_call_reentry() {
    for (label, fixture, source) in [
        (
            "late const callable",
            split_fixture as fn(&'static str) -> Fixture,
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
        ),
        (
            // Split modules preserve the early TDZ and the later read.
            "plain dynamic record",
            split_fixture,
            r#"import { ready } from "./peer.js";
export function readLater() { return getLater(); }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = { ready: "ready" };
const getLater = () => dynamic;
const later = 42;
console.log(globalThis.earlyRead, readLater() === dynamic);
"#,
        ),
        (
            "late class intermediate",
            class_split_fixture,
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
        ),
    ] {
        println!("case: {label}");
        let fixture = fixture(source);
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

const FRESH_IIFE_BEFORE_INITIALIZATION: &str = r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = ((object) => {
  object[(object.key = 0)] = "ready";
  return object;
})({});
const later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#;

#[test]
fn split_rejects_const_tdz_observed_during_unknown_call() {
    for (label, source) in [
        (
            "without IIFE writes",
            r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
const later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
        ),
        ("with fresh IIFE writes", FRESH_IIFE_BEFORE_INITIALIZATION),
    ] {
        println!("case: {label}");
        expect_independent_later_conflict(source, PEER);
    }
}

#[test]
fn colocated_unknown_call_preserves_const_tdz_before_later_read() {
    expect_independent_later_colocated_runtime(
        FRESH_IIFE_BEFORE_INITIALIZATION,
        "threw undefined 42\n",
    );
}

#[test]
fn split_rejects_var_read_before_its_assignment() {
    let source = r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
var later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#;
    expect_independent_later_conflict(source, PEER);
    expect_independent_later_colocated_runtime(source, "returned undefined 42\n");
}

#[test]
fn split_accepts_later_initialized_before_unknown_call() {
    for (label, source) in [
        (
            "var",
            r#"import { ready } from "./peer.js";
var later = 42;
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
        ),
        (
            "const",
            r#"import { ready } from "./peer.js";
const later = 42;
export function readLater() { return later; }
const prefix = ready && globalThis.runBeforeInitialization();
const dynamic = {};
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#,
        ),
    ] {
        println!("case: {label}");
        let fixture = independent_later_fixture(source);
        assert_preserved_entry_output(&fixture, "returned 42 42\n");
        assert_generated_module_after_entry_script(
            &fixture,
            "const later = await import('./static/provider/modules/values/later.js');\n\
             console.log(later.readLater());\n",
            "42\n",
        );
    }
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
    let source = r#"import { ready } from "./peer.js";
export function readLater() { return later; }
const capturedReader = readLater;
const prefix = ready && globalThis.runBeforeInitialization(capturedReader);
const dynamic = {};
const later = 42;
console.log(globalThis.earlyRead, globalThis.earlyValue, later);
"#;
    expect_independent_later_alias_conflict(source);
    expect_alias_colocated_runtime(source, "threw undefined 42\n");
}

#[test]
fn no_sync_callback_member_hint_keeps_stored_provider_deferred() {
    // The initialized, unique-writer registry alias retains its import origin
    // for lookup reach; the call hint only exempts its stored argument graph.
    let fixture = run_fixture(registered_provider_opts(true));

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
        registered_provider_opts(false),
        &["values/later"],
        &["sequenced"],
    );
}
