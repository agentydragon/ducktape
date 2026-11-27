//! Fresh local class own-field reads stay pure while getter receivers keep their publication.

use super::{execution, members};
use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;

#[test]
fn fresh_local_class_own_field_read_has_pure_value() {
    let source = r#"class Box {
  constructor(record) { this.record = record; }
  value = () => this.record.callback();
}

function make(record) { return new Box(record); }
console.log('early');
const first = make({ callback: () => 1 });
const readers = [first.value];
const later = readers;
console.log(later.length, later[0]());
"#;
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![
            logical_module("factory", &members("make", &["Box", "make"])),
            logical_module("commands", &[Member::new("first")]),
            logical_module("readers", &[Member::new("readers")]),
            logical_module_with_anon(
                "early_late",
                &[Member::new("later")],
                &["console.log('early');"],
            ),
        ],
    ));
    assert_preserved_entry_output(&fixture, "early\n1 1\n");
    assert_eq!(fixture.translation_validation()["accepts"], true);
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/readers.js",
        &["readers"],
        &[],
    );
    let graph = fixture.dependency_graph();
    let readers = node_declaring(&graph, "readers");
    assert!(readers.purity.is_pure(), "{:?}", readers.purity);
}

#[test]
fn private_own_field_purity_refuses_mutation_getters_escape_and_skipped_reads() {
    let cases = [
        (
            "const changed = first.value = () => 8;",
            "[first.value]",
            "8\n",
        ),
        (
            "const changed = Object.defineProperty(first, 'value', { get() { return () => 9; } });",
            "[first.value]",
            "9\n",
        ),
        (
            "const changed = delete first.value;",
            "[first.value]",
            "9\n",
        ),
        (
            "const changed = (globalThis.saved = first, globalThis.saved.value = () => 8);",
            "[first.value]",
            "8\n",
        ),
        (
            "const changed = (() => { const invoke = f => f(first); invoke(x => { delete x.value; }); return 1; })();",
            "[first.value]",
            "9\n",
        ),
        (
            "const changed = Date.now() < 0;",
            "changed ? [first.value] : []",
            "0\n",
        ),
        (
            "const changed = Date.now() < 0;",
            "changed && [first.value]",
            "0\n",
        ),
        (
            "let changed = Date.now() < 0;",
            "changed &&= [first.value]",
            "0\n",
        ),
        ("const changed = null;", "changed?.[first.value]", "0\n"),
        (
            "let changed;",
            "({ changed = first.value } = { changed: [] })",
            "0\n",
        ),
    ];
    for (prefix, read, expected) in cases {
        let source = format!(
            r#"class Box {{
  constructor(record) {{ this.record = record; }}
  get value() {{ return () => 9; }}
  value = () => this.record.callback();
}}
function make(record) {{ return new Box(record); }}
const first = make({{ callback: () => 1 }});
{prefix}
const readers = {read};
console.log(typeof readers?.[0] === 'function' ? readers[0]() : 0);
"#
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "all",
                    &members("make", &["Box", "make", "first", "changed", "readers"]),
                )],
            )
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, expected);
        let graph = fixture.dependency_graph();
        let readers = node_declaring(&graph, "readers");
        assert!(
            !readers.purity.is_pure(),
            "{prefix} / {read}: {:?}",
            readers.purity
        );
    }
}

#[test]
fn reading_a_constructed_own_field_does_not_publish_an_unrelated_getter_receiver() {
    let source = r#"class Holder {
  constructor(record) { this.record = record; }
  get unrelated() { return this.record.callback(); }
  search = () => this.record.callback();
}

function create(record) { return new Holder(record); }
const first = create({ callback: () => 1 });
const searches = [first.search];
const second = create({ callback: () => 2 });
console.log(searches[0](), second.search());
"#;
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            source,
            vec![
                logical_module("factory", &members("create", &["Holder", "create"])),
                logical_module("commands", &binding_members(["first", "second"])),
                logical_module("searches", &[Member::new("searches")]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_preserved_entry_output(&fixture, "1 2\n");
    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "second");
    assert!(execution.complete, "{execution:#?}");
    assert_eq!(
        execution.local_record_factory_uses.len(),
        1,
        "{execution:#?}"
    );
    let second = node_declaring(&graph, "second");
    let searches = node_declaring(&graph, "searches");
    assert!(
        !graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && edge.source == second.id
                && edge.target == searches.id
        }),
        "the private own-field read must not sequence the later factory: {:#?}",
        graph.edges
    );
}

/// Runs `source` colocated in one module and asserts that the getter-receiver
/// publication before `second` refuses its record-factory proof.
fn assert_second_factory_refused(
    source: &str,
    names: &[&'static str],
    statements: &[&str],
    dataflow_aware: bool,
    expected_output: &str,
) {
    let mut opts = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &members("create", names),
            statements,
        )],
    );
    if dataflow_aware {
        opts = opts.with_dataflow_aware_s_chain();
    }
    let fixture = run_fixture_with_graphs(opts);
    assert_preserved_entry_output(&fixture, expected_output);
    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "second");
    assert!(
        !execution.local_record_factory_rejections.is_empty(),
        "{source}: {execution:#?}"
    );
}

#[test]
fn replaced_own_field_keeps_getter_receiver_publication() {
    for replace in [
        "Object.defineProperty(first, 'search', { get() { globalThis.retained = this; return () => 1; } });",
        "delete first.search;",
        "delete ({ first }).first.search;",
        "delete ({ [first]: 1, target: first }).target.search;",
        "delete (() => first)().search;",
        "globalThis.retained = first, delete globalThis.retained.search;",
    ] {
        let source = format!(
            r#"class Holder {{
  constructor(record) {{ this.record = record; }}
  get search() {{ globalThis.retained = this; return () => 1; }}
  search = () => this.record.callback();
}}
function create(record) {{ return new Holder(record); }}
const first = create({{ callback: () => 1 }});
{replace}
const searches = [first.search];
const second = create({{ callback: () => 2 }});
console.log(searches[0](), second.search());
"#
        );
        assert_second_factory_refused(
            &source,
            &["Holder", "create", "first", "searches", "second"],
            &[replace],
            true,
            "1 2\n",
        );
    }
}

#[test]
fn reading_an_inherited_getter_still_publishes_its_receiver() {
    let source = r#"class Holder {
  constructor(record) { this.record = record; }
  get search() { globalThis.retained = this; return () => this.record.callback(); }
}
function create(record) { return new Holder(record); }
const first = create({ callback: () => 1 });
const searches = [first.search];
const second = create({ callback: () => 2 });
console.log(searches[0](), second.search());
"#;
    assert_second_factory_refused(
        source,
        &["Holder", "create", "first", "searches", "second"],
        &[],
        false,
        "1 2\n",
    );
}

#[test]
fn a_single_lexical_read_inside_a_function_can_run_after_its_field_exposes_this() {
    let source = r#"class Holder {
  constructor(record) { this.record = record; }
  get search() { globalThis.retained = this; return () => this; }
  search = () => this;
}
function create(record) { return new Holder(record); }
const first = create({ callback: () => 1 });
function read() { return first.search; }
const alias = read()();
delete alias.search;
const searches = [read()];
const second = create({ callback: () => 2 });
console.log(searches[0]() === alias, globalThis.retained === alias);
"#;
    assert_second_factory_refused(
        source,
        &[
            "Holder", "create", "first", "read", "alias", "searches", "second",
        ],
        &["delete alias.search"],
        true,
        "true true\n",
    );
}
