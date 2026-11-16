//! An opaque call cannot invoke a module-local callable that has not escaped.

use debundle_e2e_support::*;

fn expect_local_factory_late_read_conflict(source: &str) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "values/dynamic",
                    &[Member::new("dynamic"), Member::new("prefix")],
                ),
                logical_module(
                    "values/later",
                    &[
                        Member::new("later"),
                        Member::new("holder"),
                        Member::new("retain"),
                    ],
                ),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );
}

fn assert_schedule_refusal_preserves_source_and_candidate(
    opts: FixtureOpts<'_>,
    expected_output: &str,
    deferred_reader: &str,
) -> RejectedFixture {
    let rejected = run_rejection_fixture_with_graphs_and_args(opts, &["--emit-on-rejection"], 3);
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "expected a conservative evaluation-schedule refusal for deferred reader {deferred_reader}:\n{}",
        rejected.stderr
    );
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        expected_output,
        "",
    );
    assert_node_output(&rejected.entry_path(), expected_output, "");
    rejected
}

#[test]
fn unpublished_constructor_result_with_enum_array_split_stays_unproved() {
    let source = r#"var Flags = ((values) => ((values[(values.FIRST = 1)] = "FIRST"), (values[(values.SECOND = 2)] = "SECOND"), values))(Flags || {});
class Holder {
  constructor(record, policy, callback) {
    this.record = record;
    this.policy = policy;
    this.callback = callback;
    this.weight = record.weight ?? 0;
  }
  run() { return this.record.execute() + this.callback(); }
}
class Policy { allowed() { return true; } }
function create(record) { return new Holder(record, new Policy(), () => later); }
const prefix = new globalThis.Date();
const command = create({ constraints: [Flags.FIRST, Flags.SECOND], execute: () => later });
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = 42;
console.log(later);
console.log(command.run(), command.weight, command.record.constraints.length);
"#;
    let split = FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values/dynamic",
                &[
                    Member::new("Flags"),
                    Member::new("dynamic"),
                    Member::new("prefix"),
                    Member::new("command"),
                    Member::new("create"),
                    Member::new("Holder"),
                    Member::new("Policy"),
                ],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    );
    let rejected =
        assert_schedule_refusal_preserves_source_and_candidate(split, "42\n84 0 2\n", "command");
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/dynamic.js",
        &["command"],
        &["later"],
    );
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/later.js",
        &["later"],
        &["dynamic"],
    );
    assert_module_source(
        &rejected.app_root(),
        "static/app/modules/values/dynamic.js",
        &["class Holder", "class Policy", "function create"],
        &[],
    );
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "values/all",
            &[
                Member::new("Flags"),
                Member::new("Holder"),
                Member::new("Policy"),
                Member::new("create"),
                Member::new("prefix"),
                Member::new("command"),
                Member::new("dynamic"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&colocated, "42\n84 0 2\n");
}

#[test]
fn enum_array_constructor_rejects_mutation_escape_and_non_dense_inputs() {
    for (case, enum_change, constraints, after_command) in [
        (
            "getter",
            "Object.defineProperty(Flags, 'FIRST', { get() { return 1; } });",
            "[Flags.FIRST]",
            "",
        ),
        (
            "proxy",
            "Flags = new Proxy(Flags, { get() { return 1; } });",
            "[Flags.FIRST]",
            "",
        ),
        (
            "alias_getter",
            "const alias = Flags; Object.defineProperty(alias, 'FIRST', { get() { return 1; } });",
            "[Flags.FIRST]",
            "",
        ),
        ("unknown_key", "", "[Flags.MISSING]", ""),
        (
            "inherited_getter",
            "Object.defineProperty(Object.prototype, 'MISSING', { get() { return 1; } });",
            "[Flags.MISSING]",
            "",
        ),
        ("read_before_init", "", "[Flags.FIRST]", ""),
        (
            "enum_escape",
            "globalThis.savedFlags = Flags;",
            "[Flags.FIRST]",
            "",
        ),
        (
            "result_escape",
            "",
            "[Flags.FIRST]",
            "globalThis.savedCommand = command;",
        ),
        ("array_hole", "", "[, Flags.FIRST]", ""),
        ("array_spread", "", "[...[], Flags.FIRST]", ""),
    ] {
        let initializer = "var Flags = ((values) => ((values[(values.FIRST = 1)] = \"FIRST\"), values))(Flags || {});";
        let source = r#"EARLY_ENUM_DECL
ENUM_CHANGE
class Holder {
  constructor(record) { this.record = record; }
  run() { return this.record.execute(); }
}
function create(record) { return new Holder(record); }
const prefix = new globalThis.Date();
const command = create({ constraints: ARRAY_VALUE, execute: () => later });
AFTER_COMMAND
LATE_ENUM_DECL
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
"#
        .replace(
            "EARLY_ENUM_DECL",
            if case == "read_before_init" {
                ""
            } else {
                initializer
            },
        )
        .replace("ENUM_CHANGE", enum_change)
        .replace("ARRAY_VALUE", constraints)
        .replace("AFTER_COMMAND", after_command)
        .replace(
            "LATE_ENUM_DECL",
            if case == "read_before_init" {
                initializer
            } else {
                ""
            },
        );
        eprintln!("checking {case}");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "values/dynamic",
                        &[
                            Member::new("Flags"),
                            Member::new("prefix"),
                            Member::new("dynamic"),
                        ],
                    ),
                    logical_module(
                        "values/later",
                        &[
                            Member::new("Holder"),
                            Member::new("create"),
                            Member::new("command"),
                            Member::new("later"),
                        ],
                    ),
                ],
            ),
            &["values/dynamic", "values/later"],
            &[],
        );
    }
}

#[test]
fn unescaped_function_declaration_does_not_root_its_lazy_read() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const prefix = new globalThis.Date();
function readLater() { return later; }
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
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

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/dynamic.js",
        &["dynamic"],
        &["later"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/later.js",
        &["later"],
        &["dynamic"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/values/later.js",
        &["function readLater"],
        &[],
    );
    assert_preserved_entry_output(&fixture, "1\n");
}

#[test]
fn unpublished_local_constructor_result_split_stays_unproved() {
    let source = r#"class Holder {
  constructor(record, policy) {
    this.record = record;
    this.policy = policy;
    this.weight = record.weight ?? 0;
  }
  run() { return this.record.execute(); }
}
class Policy { allowed() { return true; } }
function create(record) { return new Holder(record, new Policy()); }
const prefix = new globalThis.Date();
const command = create({ execute: () => later });
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = 42;
console.log(later);
console.log(command.run(), command.weight);
"#;
    let split = FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values/dynamic",
                &[
                    Member::new("dynamic"),
                    Member::new("prefix"),
                    Member::new("command"),
                    Member::new("create"),
                    Member::new("Holder"),
                    Member::new("Policy"),
                ],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    );
    let rejected =
        assert_schedule_refusal_preserves_source_and_candidate(split, "42\n42 0\n", "command");

    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/dynamic.js",
        &["command"],
        &["later"],
    );
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/later.js",
        &["later"],
        &["dynamic"],
    );
    assert_module_source(
        &rejected.app_root(),
        "static/app/modules/values/dynamic.js",
        &["class Holder", "class Policy", "function create"],
        &[],
    );
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "values/all",
            &[
                Member::new("Holder"),
                Member::new("Policy"),
                Member::new("create"),
                Member::new("prefix"),
                Member::new("command"),
                Member::new("dynamic"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&colocated, "42\n42 0\n");
}

#[test]
fn published_local_constructor_result_keeps_deferred_read() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"class Holder {
  constructor(record) { this.record = record; }
  run() { return this.record.execute(); }
}
function create(record) { return new Holder(record); }
const prefix = new globalThis.Date();
const command = create({ execute: () => later });
globalThis.saved = command;
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
"#,
            vec![
                logical_module(
                    "values/dynamic",
                    &[Member::new("prefix"), Member::new("dynamic")],
                ),
                logical_module(
                    "values/later",
                    &[
                        Member::new("Holder"),
                        Member::new("create"),
                        Member::new("command"),
                        Member::new("later"),
                    ],
                ),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn called_local_constructor_result_keeps_deferred_read() {
    let source = r#"let later = 0;
class Holder {
  constructor(record) { this.record = record; }
  run() { return this.record.execute(); }
}
function create(record) { return new Holder(record); }
const command = create({ execute: () => later });
const early = command.run();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
later = 42;
console.log(early, later);
"#;
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("Holder"),
                    Member::new("create"),
                    Member::new("command"),
                    Member::new("early"),
                    Member::new("dynamic"),
                    Member::new("later"),
                ],
                &["later = 42;", "console.log(early, later);"],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "0 42\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "values/dynamic",
                    &[
                        Member::new("Holder"),
                        Member::new("create"),
                        Member::new("command"),
                        Member::new("early"),
                        Member::new("dynamic"),
                    ],
                ),
                logical_module_with_anon(
                    "values/later",
                    &[Member::new("later")],
                    &["later = 42;", "console.log(early, later);"],
                ),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn visible_object_prototype_getter_keeps_constructor_result_conservative() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"let later = 0;
Object.defineProperty(Object.prototype, "weight", {
  get() { return later; }, configurable: true,
});
class Holder {
  constructor(record) { this.record = record; this.weight = record.weight ?? 0; }
  run() { return this.record.execute(); }
}
function create(record) { return new Holder(record); }
const command = create({ execute: () => later });
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
later = 42;
"#,
            vec![
                logical_module(
                    "values/dynamic",
                    &[
                        Member::new("Holder"),
                        Member::new("create"),
                        Member::new("command"),
                        Member::new("dynamic"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["values/dynamic", "values/later"],
        &[],
    );
}

#[test]
fn unpublished_local_factory_result_split_stays_unproved() {
    let source = r#"function retain(read, write) {
  async function guarded() { return read(); }
  return { read, write, guarded, preview: async () => read() };
}

const prefix = new globalThis.Date();
const holder = retain(() => later, () => 0);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
console.log(holder.read());
"#;
    let split = FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values/dynamic",
                &[Member::new("dynamic"), Member::new("prefix")],
            ),
            logical_module(
                "values/later",
                &[
                    Member::new("later"),
                    Member::new("holder"),
                    Member::new("retain"),
                ],
            ),
        ],
    );
    let rejected =
        assert_schedule_refusal_preserves_source_and_candidate(split, "1\n1\n", "holder");

    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/dynamic.js",
        &["dynamic"],
        &["later"],
    );
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/later.js",
        &["later", "holder"],
        &["dynamic"],
    );
    assert_module_source(
        &rejected.app_root(),
        "static/app/modules/values/later.js",
        &["function retain", "async function guarded"],
        &[],
    );
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "values/all",
            &[
                Member::new("retain"),
                Member::new("prefix"),
                Member::new("holder"),
                Member::new("dynamic"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&colocated, "1\n1\n");
}

#[test]
fn fresh_local_record_field_assignment_split_stays_unproved() {
    let source = r#"function wrap(callback) {
  const result = { run: null };
  result.run = callback;
  return result;
}
const prefix = new globalThis.Date();
const holder = wrap(() => later);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later, holder.run());
"#;
    let split = FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values/dynamic",
                &[Member::new("dynamic"), Member::new("prefix")],
            ),
            logical_module(
                "values/later",
                &[
                    Member::new("later"),
                    Member::new("holder"),
                    Member::new("wrap"),
                ],
            ),
        ],
    );
    assert_schedule_refusal_preserves_source_and_candidate(split, "1 1\n", "holder");
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "values/all",
            &[
                Member::new("wrap"),
                Member::new("prefix"),
                Member::new("holder"),
                Member::new("dynamic"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&colocated, "1 1\n");
}

#[test]
fn distinct_own_field_writes_split_stays_unproved() {
    let source = r#"function wrap(callback, tag) {
  const result = { run: null, tag: 0 };
  result.run = callback;
  result.tag = tag;
  return result;
}
const prefix = new globalThis.Date();
const holder = wrap(() => later, 2);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later, holder.run(), holder.tag);
"#;
    let split = FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values/dynamic",
                &[Member::new("dynamic"), Member::new("prefix")],
            ),
            logical_module(
                "values/later",
                &[
                    Member::new("later"),
                    Member::new("holder"),
                    Member::new("wrap"),
                ],
            ),
        ],
    );
    assert_schedule_refusal_preserves_source_and_candidate(split, "1 1 2\n", "holder");
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "values/all",
            &[
                Member::new("wrap"),
                Member::new("prefix"),
                Member::new("holder"),
                Member::new("dynamic"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&colocated, "1 1 2\n");
}

#[test]
fn record_writes_without_closed_own_field_ownership_keep_the_cycle() {
    for body in [
        "const result = { get run() { return callback(); } }; result.run = callback; return result;",
        "const result = { set run(value) { value(); } }; result.run = callback; return result;",
        "const result = {}; result.run = callback; return result;",
        "const result = { run: null }; globalThis.saved = result; result.run = callback; return result;",
        "const result = { run: null }; const alias = result; alias.run = callback; return result;",
        "const result = { run: null }; callback(); result.run = callback; return result;",
        "const result = { run: null }; result.run = callback; result.run = callback; return result;",
        "const result = { run: null }; result['run'] = callback; return result;",
    ] {
        let source = format!(
            "function wrap(callback) {{ {body} }}\n\
const prefix = new globalThis.Date();\n\
const holder = wrap(() => later);\n\
const dynamic = ((object) => {{ object[(object.key = 0)] = 'value'; return 1; }})({{}});\n\
const later = dynamic;\n"
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "values/dynamic",
                        &[Member::new("dynamic"), Member::new("prefix")],
                    ),
                    logical_module(
                        "values/later",
                        &[
                            Member::new("later"),
                            Member::new("holder"),
                            Member::new("wrap"),
                        ],
                    ),
                ],
            ),
            &["values/dynamic", "values/later"],
            &[],
        );
    }
}

#[test]
fn published_local_factory_result_keeps_later_read() {
    let source = r#"function retain(read) { return { read }; }
const prefix = new globalThis.Date();
const holder = retain(() => later);
globalThis.saved = holder;
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
"#;
    expect_local_factory_late_read_conflict(source);
}

#[test]
fn called_local_factory_result_keeps_later_read() {
    let source = r#"function retain(read) { return { read }; }
const prefix = new globalThis.Date();
const holder = retain(() => later);
holder.read();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
"#;
    expect_local_factory_late_read_conflict(source);
}

#[test]
fn reassigned_local_factory_keeps_conservative_reachability() {
    let source = r#"function retain(read) { return { read }; }
retain = function replacement(read) { return { read }; };
const prefix = new globalThis.Date();
const holder = retain(() => later);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
"#;
    expect_local_factory_late_read_conflict(source);
}

#[test]
fn accessor_return_from_local_factory_keeps_conservative_reachability() {
    let source = r#"function retain(read) { return { get read() { return read(); } }; }
const prefix = new globalThis.Date();
const holder = retain(() => later);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
"#;
    expect_local_factory_late_read_conflict(source);
}

#[test]
fn unpublished_imported_callback_factory_split_stays_unproved() {
    let source = r#"import { observe } from './peer.js';
const prefix = new globalThis.Date();
const wrapped = observe(() => later);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
console.log(wrapped());
"#;
    let provider = "export function observe(callback) { return (...args) => callback(...args); }\n";
    let policy = ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["observe"])
        .build();
    let peers = [("static/peer", provider)];
    let split = FixtureOpts::new(
        source,
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
    .with_extra_chunks(&peers)
    .with_chunk_export_purity(std::slice::from_ref(&policy));
    let rejected =
        assert_schedule_refusal_preserves_source_and_candidate(split, "1\n1\n", "wrapped");

    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/dynamic.js",
        &["dynamic"],
        &["later"],
    );
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/later.js",
        &["later", "wrapped"],
        &["dynamic"],
    );
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "values/all",
                &[
                    Member::new("prefix"),
                    Member::new("wrapped"),
                    Member::new("dynamic"),
                    Member::new("later"),
                ],
            )],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&[policy]),
    );
    assert_preserved_entry_output(&colocated, "1\n1\n");
}

#[test]
fn published_imported_callback_factory_result_keeps_later_read() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"import { observe } from './peer.js';
const prefix = new globalThis.Date();
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
        r#"const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
console.log(later);
"#,
        unescaped_local_callable_modules(),
    ));

    assert_unescaped_local_callable_split(&fixture);
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
        unescaped_local_callable_modules(),
    ));

    assert_unescaped_local_callable_split(&fixture);
}

#[test]
fn callback_escaped_after_unknown_call_remains_a_possible_accessor() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"new globalThis.Date();
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
    for helper in [
        "function exposeArgument(callback) { consume(arguments); }",
        "function exposeArgument() { return arguments[0](); }",
    ] {
        let source = [
            helper,
            r#"exposeArgument(readLater);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
"#,
        ]
        .join("\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("values/dynamic", &[Member::new("dynamic")]),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            ),
            &["values/dynamic", "values/later"],
            &[],
        );
    }
}

#[test]
fn cyclic_importer_keeps_exported_hoisted_callable_reachable_before_evaluation() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"import "./peer.js";
new globalThis.Date();
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
new globalThis.Date();
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
new globalThis.Date();
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

#[test]
fn fresh_class_field_write_after_opaque_call_split_stays_unproved() {
    let source = r#"import { prepare } from './peer.js';
class Reader { read() { return later; } }
const reader = new Reader();
class Cache { value = 0; }
const marker = prepare();
const cache = new Cache();
cache.value = 1;
const later = 42;
console.log(cache.value, reader.read(), later);
export { Reader, reader, Cache, cache, marker, later };
"#;
    let peers = [("static/peer", "export function prepare() { return {}; }")];
    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "values/cache",
                &[
                    Member::new("Cache"),
                    Member::new("cache"),
                    Member::new("marker"),
                    Member::new("Reader"),
                    Member::new("reader"),
                ],
                &["cache.value = 1;"],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
    .with_extra_chunks(&peers)
    .with_unassigned_mode(unassigned_mode_inline());
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    let rejected =
        assert_schedule_refusal_preserves_source_and_candidate(split, "1 42 42\n", "reader");
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/cache.js",
        &["cache", "reader"],
        &["later"],
    );
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/values/later.js",
        &["later"],
        &["cache"],
    );
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values/all",
            &[
                Member::new("Cache"),
                Member::new("cache"),
                Member::new("marker"),
                Member::new("Reader"),
                Member::new("reader"),
                Member::new("later"),
            ],
            &["cache.value = 1;"],
        )],
    )
    .with_extra_chunks(&peers)
    .with_unassigned_mode(unassigned_mode_inline());
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    let fixture = run_fixture(colocated);
    assert_preserved_entry_output(&fixture, "1 42 42\n");
}

#[test]
fn fresh_class_instance_without_following_field_write_keeps_method_deferred() {
    let source = r#"import { prepare } from './peer.js';
class Reader { read() { return later; } }
const reader = new Reader();
class Cache { value = 0; }
const marker = prepare();
const cache = new Cache();
const later = 42;
console.log(cache.value, reader.read(), later);
export { Reader, reader, Cache, cache, marker, later };
"#;
    let peers = [("static/peer", "export function prepare() { return {}; }")];
    let mut fixture = FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values/cache",
                &[
                    Member::new("Cache"),
                    Member::new("cache"),
                    Member::new("marker"),
                    Member::new("Reader"),
                    Member::new("reader"),
                ],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
    .with_extra_chunks(&peers)
    .with_unassigned_mode(unassigned_mode_inline());
    fixture.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(fixture), "0 42 42\n");
}

#[test]
fn replaced_own_field_descriptor_can_invoke_a_setter() {
    let source = r#"import { prepare } from './peer.js';
let later = 0;
let observed = -1;
class Cache { value = 0; }
const marker = prepare();
const cache = new Cache();
Object.defineProperty(cache, 'value', {
  set(value) { observed = later; }, configurable: true,
});
cache.value = 1;
later = 42;
console.log(observed, later);
export { Cache, cache, marker, later, observed };
"#;
    let peers = [("static/peer", "export function prepare() { return {}; }")];
    let mut colocated = FixtureOpts::new(source, vec![])
        .with_extra_chunks(&peers)
        .with_unassigned_mode(unassigned_mode_inline());
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "values/cache",
                &[
                    Member::new("Cache"),
                    Member::new("cache"),
                    Member::new("marker"),
                    Member::new("observed"),
                ],
                &[
                    "Object.defineProperty(cache, 'value', { set(value) { observed = later; }, configurable: true });",
                    "cache.value = 1;",
                ],
            ),
            logical_module_with_anon(
                "values/later",
                &[Member::new("later")],
                &["later = 42;"],
            ),
        ],
    )
    .with_extra_chunks(&peers)
    .with_unassigned_mode(unassigned_mode_inline());
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    expect_atomic_conflict_rejection(split, &["values/cache", "values/later"], &[]);
}

#[test]
fn deleted_class_field_exposes_inherited_setter() {
    let source = r#"import { prepare } from './peer.js';
let later = 0;
let observed = -1;
class Cache { value = 0; }
Object.defineProperty(Cache.prototype, 'value', {
  set(value) { observed = later; }, configurable: true,
});
const marker = prepare();
const cache = new Cache();
delete cache.value;
cache.value = 1;
later = 42;
console.log(observed, later);
export { Cache, cache, marker, later, observed };
"#;
    let peers = [("static/peer", "export function prepare() { return {}; }")];
    let mut colocated = FixtureOpts::new(source, vec![])
        .with_extra_chunks(&peers)
        .with_unassigned_mode(unassigned_mode_inline());
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "values/cache",
                &[
                    Member::new("Cache"),
                    Member::new("cache"),
                    Member::new("marker"),
                    Member::new("observed"),
                ],
                &[
                    "Object.defineProperty(Cache.prototype, 'value', { set(value) { observed = later; }, configurable: true });",
                    "delete cache.value;",
                    "cache.value = 1;",
                ],
            ),
            logical_module_with_anon(
                "values/later",
                &[Member::new("later")],
                &["later = 42;"],
            ),
        ],
    )
    .with_extra_chunks(&peers)
    .with_unassigned_mode(unassigned_mode_inline());
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    expect_atomic_conflict_rejection(split, &["values/cache", "values/later"], &[]);
}
