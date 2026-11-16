//! Compare source chunks with real CLI output across local and imported calls.

use debundle_e2e_graph_support::effect_validation::binding_execution_report;
use debundle_e2e_support::*;

const ICON_SOURCE: &str = r#"import { field, node } from "./peer.js";
field(() => later);
const icon = node("svg");
const later = () => icon;
console.log(globalThis.get()());
"#;

const ICON_PEER: &str = r#"export const field = (callback) => globalThis.get = callback;
export const node = (tag) => tag;
"#;

fn provider_module(members: &[&'static str]) -> Vec<LogicalModuleEntry> {
    let members = members
        .iter()
        .map(|name| Member::new(name))
        .collect::<Vec<_>>();
    vec![logical_module("provider", &members)]
}

fn icon_split() -> ProgramPlacement<'static> {
    icon_placement(ICON_SOURCE).add_chunk(
        "static/peer",
        ICON_PEER,
        provider_module(&["field", "node"]),
    )
}

fn icon_placement(source: &'static str) -> ProgramPlacement<'static> {
    ProgramPlacement::new(source).place_in_entry(logical_module("icons", &[Member::new("icon")]))
}

fn local_icon_fixture(source: &'static str) -> Fixture {
    icon_placement(source).run()
}

fn after_statement_placement(source: &'static str) -> ProgramPlacement<'static> {
    ProgramPlacement::new(source)
        .with_unassigned_mode(unassigned_mode_inline())
        .place_in_entry(logical_module_with_anon(
            "after",
            &[],
            &["console.log('after');"],
        ))
}

fn moved_binding_placement(source: &'static str, name: &'static str) -> ProgramPlacement<'static> {
    ProgramPlacement::new(source)
        .with_unassigned_mode(unassigned_mode_inline())
        .place_in_entry(logical_module("moved", &[Member::new(name)]))
}

fn assert_gate_rejects_output_divergence(
    placement: ProgramPlacement<'static>,
    source_output: &str,
    emitted_output: &str,
) {
    let rejected = placement.emit_on_rejection();
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        source_output,
        "",
    );
    assert_node_output(&rejected.entry_path(), emitted_output, "");
    let assessment = rejected.candidate_relocation_assessment();
    assert_eq!(assessment["legacy_gate_accepts"], false, "{assessment:#}");
}

fn assert_icon_split(fixture: &Fixture) {
    assert_preserved_entry_output(fixture, "svg\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/icons.js",
        &["icon"],
        &["later"],
    );
}

#[test]
fn imported_shared_provider_preserves_the_icon_only_extraction() {
    let fixture = icon_split().run();
    assert_icon_split(&fixture);
    assert_module_exports(
        &fixture.out_root,
        "static/peer/entry.js",
        &["field", "node"],
        &[],
    );
}

#[test]
fn local_hoisted_helpers_are_a_control_for_the_imported_callback_case() {
    let source = r#"function field(callback) { globalThis.get = callback; }
function node(tag) { return tag; }
field(() => later);
const icon = node("svg");
const later = () => icon;
console.log(globalThis.get()());
"#;
    let fixture = local_icon_fixture(source);
    assert_icon_split(&fixture);
}

#[test]
fn local_consumer_sharing_the_provider_preserves_the_same_icon_split() {
    let source = r#"function field(callback) { globalThis.get = callback; }
function node(tag) { return tag; }
function register(callback) { field(callback); }
field(() => later);
const icon = node("svg");
const later = () => icon;
register(() => later);
console.log(globalThis.get()());
"#;
    let fixture = local_icon_fixture(source);
    assert_icon_split(&fixture);
}

#[test]
fn local_alias_to_a_reexported_provider_preserves_the_same_icon_split() {
    let source = r#"function field(callback) { globalThis.get = callback; }
function node(tag) { return tag; }
const fieldFromRelay = field;
fieldFromRelay(() => later);
const icon = node("svg");
const later = () => icon;
console.log(globalThis.get()());
"#;
    let fixture = local_icon_fixture(source);
    assert_icon_split(&fixture);
}

#[test]
fn local_namespace_object_preserves_the_same_icon_split() {
    let source = r#"function field(callback) { globalThis.get = callback; }
function node(tag) { return tag; }
const peer = { field, node };
peer.field(() => later);
const icon = peer.node("svg");
const later = () => icon;
console.log(globalThis.get()());
"#;
    let fixture = local_icon_fixture(source);
    assert_icon_split(&fixture);
}

#[test]
fn a_provider_shared_by_entry_and_another_importer_keeps_its_callback() {
    let source = r#"import { field, node } from "./peer.js";
import { register } from "./consumer.js";
field(() => later);
const icon = node("svg");
const later = () => icon;
register(() => later);
console.log(globalThis.get()());
"#;
    let fixture = ProgramPlacement::new(source)
        .place_in_entry(logical_module("icons", &[Member::new("icon")]))
        .add_chunk(
            "static/peer",
            ICON_PEER,
            provider_module(&["field", "node"]),
        )
        .add_chunk(
            "static/consumer",
            "import { field } from './peer.js';\nexport const register = callback => field(callback);\n",
            provider_module(&["register"]),
        )
        .run();
    assert_icon_split(&fixture);
    assert_module_exports(
        &fixture.out_root,
        "static/peer/entry.js",
        &["field", "node"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/consumer/entry.js",
        &["register"],
        &[],
    );
}

#[test]
fn separate_providers_keep_imported_calls_and_safe_const_availability() {
    let source = r#"import { field } from "./callbacks.js";
import { node } from "./nodes.js";
field(() => later);
const icon = node("svg");
const later = () => icon;
console.log(globalThis.get()());
"#;
    let fixture = ProgramPlacement::new(source)
        .place_in_entry(logical_module("icons", &[Member::new("icon")]))
        .add_chunk(
            "static/callbacks",
            "export const field = callback => globalThis.get = callback;\n",
            provider_module(&["field"]),
        )
        .add_chunk(
            "static/nodes",
            "export const node = tag => tag;\n",
            provider_module(&["node"]),
        )
        .run();
    assert_icon_split(&fixture);
    assert_module_exports(
        &fixture.out_root,
        "static/callbacks/entry.js",
        &["field"],
        &[],
    );
    assert_module_exports(&fixture.out_root, "static/nodes/entry.js", &["node"], &[]);
}

#[test]
fn a_reexported_provider_keeps_the_imported_callback_and_value() {
    let source = r#"import { field } from "./relay.js";
import { node } from "./peer.js";
field(() => later);
const icon = node("svg");
const later = () => icon;
console.log(globalThis.get()());
"#;
    let fixture = ProgramPlacement::new(source)
        .place_in_entry(logical_module("icons", &[Member::new("icon")]))
        .add_chunk(
            "static/peer",
            ICON_PEER,
            provider_module(&["field", "node"]),
        )
        .add_chunk(
            "static/relay",
            "export { field } from './peer.js';\n",
            vec![],
        )
        .run();
    assert_icon_split(&fixture);
    assert_module_exports(
        &fixture.out_root,
        "static/peer/entry.js",
        &["field", "node"],
        &[],
    );
    assert_module_exports(&fixture.out_root, "static/relay/entry.js", &["field"], &[]);
}

#[test]
fn namespace_provider_references_keep_the_imported_callback_and_value() {
    let source = r#"import * as peer from "./peer.js";
peer.field(() => later);
const icon = peer.node("svg");
const later = () => icon;
console.log(globalThis.get()());
"#;
    let fixture = ProgramPlacement::new(source)
        .place_in_entry(logical_module("icons", &[Member::new("icon")]))
        .add_chunk(
            "static/peer",
            ICON_PEER,
            provider_module(&["field", "node"]),
        )
        .run();
    assert_icon_split(&fixture);
    assert_module_exports(
        &fixture.out_root,
        "static/peer/entry.js",
        &["field", "node"],
        &[],
    );
}

#[test]
fn returned_record_method_capture_survives_a_local_alias() {
    let source = r#"import { make } from "./records.js";
import { node } from "./nodes.js";
const record = make(() => later);
const read = record.read;
const icon = node("svg");
const later = () => icon;
console.log(read()());
"#;
    let fixture = ProgramPlacement::new(source)
        .place_in_entry(logical_module("icons", &[Member::new("icon")]))
        .add_chunk(
            "static/records",
            "export function make(callback) { return { read() { return callback(); } }; }\n",
            provider_module(&["make"]),
        )
        .add_chunk(
            "static/nodes",
            "export const node = tag => tag;\n",
            provider_module(&["node"]),
        )
        .run();
    assert_icon_split(&fixture);
    assert_module_exports(&fixture.out_root, "static/records/entry.js", &["make"], &[]);
    assert_module_exports(&fixture.out_root, "static/nodes/entry.js", &["node"], &[]);
}

#[test]
fn local_returned_record_capture_survives_a_local_method_alias() {
    let source = r#"function make(callback) { return { read() { return callback(); } }; }
function node(tag) { return tag; }
const record = make(() => later);
const read = record.read;
const icon = node("svg");
const later = () => icon;
console.log(read()());
"#;
    let fixture = local_icon_fixture(source);
    assert_icon_split(&fixture);
}

const REGISTRY_PROVIDER: &str = r#"export const install = callback => globalThis.saved = callback;
export const invoke = () => globalThis.saved();
"#;

#[test]
fn a_hoisted_callback_target_can_move_ahead_of_its_importing_call() {
    let source = r#"import { install, invoke } from "./registry.js";
install(() => later());
console.log(invoke());
function later() { return "ready"; }
"#;
    let fixture = ProgramPlacement::new(source)
        .place_in_entry(logical_module("functions", &[Member::new("later")]))
        .add_chunk(
            "static/registry",
            REGISTRY_PROVIDER,
            provider_module(&["install", "invoke"]),
        )
        .run();
    assert_preserved_entry_output(&fixture, "ready\n");
    assert_module_exports(
        &fixture.out_root,
        "static/registry/entry.js",
        &["install", "invoke"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/functions.js",
        &["later"],
        &[],
    );
}

#[test]
fn a_const_callback_target_keeps_its_temporal_dead_zone() {
    let source = r#"console.log(later());
const later = () => "ready";
"#;
    let rejected = ProgramPlacement::new(source)
        .place_in_entry(logical_module("functions", &[Member::new("later")]))
        .emit_on_rejection();

    assert_node_error(
        &rejected.source_root.join("static/app.js"),
        "ReferenceError",
    );
    assert_node_output(&rejected.entry_path(), "ready\n", "");
    assert!(
        !rejected.atomic_group_conflicts().is_empty(),
        "candidate rejection had no atomic-group gate evidence:\n{}",
        rejected.stderr
    );
}

const IDENTICAL_MUTABLE_PROVIDER: &str = r#"const state = { value: 0 };
export { state };
export function next() { return ++state.value; }
export function peek() { return state.value; }
"#;

fn identical_provider_placement(source: &'static str) -> ProgramPlacement<'static> {
    moved_binding_placement(source, "changed")
        .add_chunk(
            "static/left",
            IDENTICAL_MUTABLE_PROVIDER,
            provider_module(&["state", "next", "peek"]),
        )
        .add_chunk(
            "static/right",
            IDENTICAL_MUTABLE_PROVIDER,
            provider_module(&["state", "next", "peek"]),
        )
}

#[test]
fn identical_provider_offsets_keep_mutable_records_chunk_local() {
    let source = r#"import { state } from "./left.js";
import { next } from "./right.js";
import { peek } from "./right.js";
const changed = next();
const after = state.value;
const seen = peek();
console.log(changed, after, seen);
"#;
    let fixture = identical_provider_placement(source).run();
    assert_preserved_entry_output(&fixture, "1 0 1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/moved.js",
        &["changed"],
        &[],
    );
}

#[test]
fn a_returned_live_closure_keeps_its_provider_capture_ordered() {
    let provider = r#"const state = { count: 0 };
export function reader() { return () => state.count; }
export function advance() { return ++state.count; }
"#;
    let source = r#"import { reader, advance } from "./peer.js";
const read = reader();
const before = read();
const advanced = advance();
const after = read();
console.log(before, advanced, after);
"#;
    let placement = moved_binding_placement(source, "advanced").add_chunk(
        "static/peer",
        provider,
        provider_module(&["reader", "advance"]),
    );
    assert_gate_rejects_output_divergence(placement, "0 1 1\n", "1 1 1\n");
}

#[test]
fn moving_a_provider_update_across_its_read_order_is_refused() {
    let provider = r#"let count = 0;
export function reader() { return () => count; }
export function advance() { return ++count; }
"#;
    let source = r#"import { reader, advance } from "./peer.js";
const read = reader();
const before = read();
const advanced = advance();
const after = read();
console.log(before, advanced, after);
"#;
    let placement = moved_binding_placement(source, "advanced").add_chunk(
        "static/peer",
        provider,
        provider_module(&["count", "reader", "advance"]),
    );
    assert_gate_rejects_output_divergence(placement, "0 1 1\n", "1 1 1\n");
}

const OBSERVER_PROVIDER: &str = r#"let count = 0;
export function next() { return ++count; }
export function observe() { return count; }
"#;

#[test]
fn namespace_observer_snapshot_stays_after_its_imported_writer() {
    let source = r#"import * as peer from "./peer.js";
const snapshot = peer.observe();
const changed = peer.next();
const after = peer.observe();
console.log(snapshot, changed, after);
"#;
    let placement = moved_binding_placement(source, "changed").add_chunk(
        "static/peer",
        OBSERVER_PROVIDER,
        provider_module(&["count", "next", "observe"]),
    );
    assert_gate_rejects_output_divergence(placement, "0 1 1\n", "1 1 1\n");
}

#[test]
fn reexported_observer_snapshot_stays_after_its_imported_writer() {
    let source = r#"import { next } from "./peer.js";
import { observe } from "./relay.js";
const snapshot = observe();
const changed = next();
const after = observe();
console.log(snapshot, changed, after);
"#;
    let placement = moved_binding_placement(source, "changed")
        .add_chunk(
            "static/peer",
            OBSERVER_PROVIDER,
            provider_module(&["count", "next", "observe"]),
        )
        .add_chunk(
            "static/relay",
            "export { observe } from './peer.js';\n",
            vec![],
        );
    assert_gate_rejects_output_divergence(placement, "0 1 1\n", "1 1 1\n");
}

const CYCLE_OBSERVER: &str = r#"import { value } from "./app.js";
export const snapshot = value;
"#;

#[test]
fn var_backedge_reads_the_instantiation_value_before_moved_initialization() {
    let source = r#"import { snapshot } from "./observer.js";
var value = "ready";
console.log(snapshot);
export { value };
"#;
    let fixture = ProgramPlacement::new(source)
        .place_in_entry(logical_module("state", &[Member::new("value")]))
        .add_chunk("static/observer", CYCLE_OBSERVER, vec![])
        .run();
    assert_preserved_entry_output(&fixture, "undefined\n");
}

#[test]
fn lexical_backedge_retains_its_source_tdz_after_partitioning() {
    let source = r#"import { snapshot } from "./observer.js";
let value = "ready";
console.log(snapshot);
export { value };
"#;
    let fixture = ProgramPlacement::new(source)
        .place_in_entry(logical_module("state", &[Member::new("value")]))
        .add_chunk("static/observer", CYCLE_OBSERVER, vec![])
        .run();
    assert_preserved_entry_error(&fixture, "Cannot access 'value' before initialization");
}

#[test]
fn imported_provider_throw_keeps_later_entry_effects_after_the_catch() {
    let source = r#"import { stop } from "./peer.js";
console.log("before");
try { stop(); } catch { console.log("caught"); }
const after = (console.log("after"), 7);
console.log(after);
"#;
    let provider = "export function stop() { console.log('provider'); throw new Error('stop'); }\n";
    let placement = moved_binding_placement(source, "after").add_chunk(
        "static/peer",
        provider,
        provider_module(&["stop"]),
    );
    assert_gate_rejects_output_divergence(
        placement,
        "before\nprovider\ncaught\nafter\n7\n",
        "after\nbefore\nprovider\ncaught\n7\n",
    );
}

#[test]
fn a_returned_shared_record_keeps_provider_mutation_ordered() {
    let provider = r#"const shared = { value: 0 };
export function getRecord() { return shared; }
export function increment() { return ++shared.value; }
"#;
    let source = r#"import { getRecord, increment } from "./peer.js";
const record = getRecord();
const before = record.value;
const changed = increment();
const after = record.value;
console.log(before, changed, after);
"#;
    let placement = moved_binding_placement(source, "changed").add_chunk(
        "static/peer",
        provider,
        provider_module(&["getRecord", "increment"]),
    );
    assert_gate_rejects_output_divergence(placement, "0 1 1\n", "1 1 1\n");
}

const SYNC_RUN_PROVIDER: &str = "export function run(callback) { return callback(); }\n";
const SYNC_GETTER_PROVIDER: &str =
    "export function wrap(callback) { return { get value() { return callback(); } }; }\n";

#[test]
fn synchronous_callbacks_stay_before_later_effects_locally_and_through_imports() {
    let local = r#"function run(callback) { return callback(); }
run(() => console.log("callback"));
console.log('after');
"#;
    let imported = r#"import { run } from "./peer.js";
run(() => console.log("callback"));
console.log('after');
"#;
    assert_gate_rejects_output_divergence(
        after_statement_placement(local),
        "callback\nafter\n",
        "after\ncallback\n",
    );
    assert_gate_rejects_output_divergence(
        after_statement_placement(imported).add_chunk(
            "static/peer",
            SYNC_RUN_PROVIDER,
            provider_module(&["run"]),
        ),
        "callback\nafter\n",
        "after\ncallback\n",
    );
}

#[test]
fn throwing_callbacks_stay_before_later_effects_locally_and_through_imports() {
    let local = r#"function run(callback) { return callback(); }
try { run(() => { console.log("callback"); throw new Error("stop"); }); }
catch { console.log("caught"); }
console.log('after');
"#;
    let imported = r#"import { run } from "./peer.js";
try { run(() => { console.log("callback"); throw new Error("stop"); }); }
catch { console.log("caught"); }
console.log('after');
"#;
    let source_output = "callback\ncaught\nafter\n";
    let emitted_output = "after\ncallback\ncaught\n";
    assert_gate_rejects_output_divergence(
        after_statement_placement(local),
        source_output,
        emitted_output,
    );
    assert_gate_rejects_output_divergence(
        after_statement_placement(imported).add_chunk(
            "static/peer",
            SYNC_RUN_PROVIDER,
            provider_module(&["run"]),
        ),
        source_output,
        emitted_output,
    );
}

#[test]
fn returned_getters_stay_before_later_effects_locally_and_through_imports() {
    let local = r#"const record = { get value() { console.log("getter"); return 1; } };
record.value;
console.log('after');
"#;
    let imported = r#"import { wrap } from "./peer.js";
const record = wrap(() => { console.log("getter"); return 1; });
record.value;
console.log('after');
"#;
    assert_gate_rejects_output_divergence(
        after_statement_placement(local),
        "getter\nafter\n",
        "after\ngetter\n",
    );
    assert_gate_rejects_output_divergence(
        after_statement_placement(imported).add_chunk(
            "static/peer",
            SYNC_GETTER_PROVIDER,
            provider_module(&["wrap"]),
        ),
        "getter\nafter\n",
        "after\ngetter\n",
    );
}

fn assert_ordered_transfer_family(
    family: &str,
    declarations: &str,
    expected: &str,
    authority: &str,
) {
    let mut local_complete = None;
    for imported in [false, true] {
        let prefix = if imported {
            "import { compute } from './provider.js';"
        } else {
            declarations
        };
        let source = format!(
            "{prefix}\nconst first = 7;\nvar second = compute();\nconsole.log(first, second);\n"
        );
        let mut members = vec![Member::new("second")];
        if !imported {
            members.push(Member::new("compute"));
        }
        let provider = format!("{declarations}\nexport {{ compute }};\n");
        let mut placement = ProgramPlacement::new(&source)
            .with_dataflow_aware_s_chain()
            .with_unassigned_mode(unassigned_mode_inline())
            .place_in_entry(logical_module("second", &members));
        if imported {
            placement = placement.add_chunk("static/provider", &provider, vec![]);
        }
        let fixture = placement.run_with_graphs();
        assert_preserved_entry_output(&fixture, expected);
        assert_module_exports(
            &fixture.out_root,
            "static/app/modules/second.js",
            &["second"],
            &["first"],
        );
        let report = binding_execution_report(&fixture, "second");
        assert_eq!(
            report["binding_effect_authority"]["authority"], authority,
            "the relocated {family} must preserve local effect authority (imported={imported}): {report:#}"
        );
        if let Some(local_complete) = &local_complete {
            assert_eq!(
                &report["complete"], local_complete,
                "the imported {family} must preserve local completion coverage: {report:#}"
            );
        } else {
            local_complete = Some(report["complete"].clone());
        }
    }
}

#[test]
fn local_and_imported_binding_updates_have_the_same_relocation_proof() {
    assert_ordered_transfer_family(
        "local initialization and numeric update",
        "function compute() { let value = 1; return ++value; }",
        "7 2\n",
        "operation_receipt",
    );
}

#[test]
fn local_and_imported_scalar_operands_have_the_same_relocation_proof() {
    assert_ordered_transfer_family(
        "sequence, binary, conditional, logical and template operands",
        "function compute() { const n = 2; const value = (n, n > 0 ? n : 0); return `${value && (value || 3)}`; }",
        "7 2\n",
        "operation_receipt",
    );
}

#[test]
fn local_and_imported_record_fields_have_the_same_relocation_proof() {
    assert_ordered_transfer_family(
        "fresh record construction, field write and field read",
        "function compute() { const n = 2; const record = { value: n }; record.value = n + 1; return record.value; }",
        "7 3\n",
        "operation_receipt",
    );
}

#[test]
fn local_and_imported_array_spread_have_the_same_conservative_coverage() {
    assert_ordered_transfer_family(
        "fresh array construction, spread and indexed read",
        "function compute() { const n = 2; const values = [n]; const copy = [...values]; return copy[0]; }",
        "7 2\n",
        "ordered_root_initializer_uncovered",
    );
}

#[test]
fn local_and_imported_captured_closure_calls_have_the_same_conservative_coverage() {
    assert_ordered_transfer_family(
        "arrow creation and parameter capture",
        "function compute() { const n = 2; const read = () => n; return read(); }",
        "7 2\n",
        "ordered_root_initializer_uncovered",
    );
}

#[test]
fn local_and_imported_hoisted_functions_have_the_same_conservative_coverage() {
    assert_ordered_transfer_family(
        "hoisted local function and parameter initialization",
        "function compute() { const n = 2; return read(n); function read(value) { return value; } }",
        "7 2\n",
        "ordered_root_initializer_uncovered",
    );
}

#[test]
fn local_and_imported_method_calls_have_the_same_conservative_coverage() {
    assert_ordered_transfer_family(
        "method creation and static member call",
        "function compute() { const n = 2; const record = { read(value) { return value; } }; return record.read(n); }",
        "7 2\n",
        "ordered_root_initializer_uncovered",
    );
}

#[test]
fn local_and_imported_binding_reassignments_have_the_same_conservative_coverage() {
    assert_ordered_transfer_family(
        "local binding reassignment",
        "function compute() { const n = 2; let value = n; value = value + 1; ++value; return value; }",
        "7 4\n",
        "ordered_root_initializer_uncovered",
    );
}
