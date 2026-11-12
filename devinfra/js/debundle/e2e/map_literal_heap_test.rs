use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;

const SOURCE: &str = r#"
const a = new Map([["a", 1]]);
const b = new Map([["b", 2]]);
const c = new Map([["c", 3]]);
const d = new Map([["d", 4]]);
console.log(a.get("a"), b.get("b"), c.get("c"), d.get("d"));
export { a, b, c, d };
"#;

fn split() -> FixtureOpts<'static> {
    FixtureOpts::new(
        SOURCE,
        vec![
            logical_module("odd", &[Member::new("a"), Member::new("c")]),
            logical_module("even", &[Member::new("b"), Member::new("d")]),
        ],
    )
}

fn assert_odd_even_map_modules(fixture: &Fixture) {
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/odd.js",
        &["a", "c"],
        &["b", "d"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/even.js",
        &["b", "d"],
        &["a", "c"],
    );
}

#[test]
fn fresh_literal_map_constructors_split_and_preserve_runtime() {
    let fixture = run_fixture(split());
    assert_preserved_entry_output(&fixture, "1 2 3 4\n");
    assert_eq!(fixture.translation_validation()["accepts"], true);
    assert_all_emitted_js_checks(&fixture);
    assert_odd_even_map_modules(&fixture);
    let graph = fixture.dependency_graph();
    let first = node_declaring(&graph, "a");
    let frontier = first
        .synchronous_execution
        .as_ref()
        .and_then(|execution| execution.heap_frontier.as_ref())
        .expect("Map heap frontier");
    assert!(!frontier.own_at_init.opaque);
    assert!(frontier.own_at_init.allocations.count >= 2);
}

#[test]
fn split_comma_list_map_declarators_keep_the_same_proof() {
    let source = r#"
const a = new Map([["a", 1]]),
      b = new Map([["b", 2]]),
      c = new Map([["c", 3]]),
      d = new Map([["d", 4]]);
console.log(a.get("a"), b.get("b"), c.get("c"), d.get("d"));
export { a, b, c, d };
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("odd", &[Member::new("a"), Member::new("c")]),
            logical_module("even", &[Member::new("b"), Member::new("d")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "1 2 3 4\n");
    assert_eq!(fixture.translation_validation()["accepts"], true);
    assert_all_emitted_js_checks(&fixture);
    assert_odd_even_map_modules(&fixture);
    let graph = fixture.dependency_graph();
    assert!(
        !node_declaring(&graph, "c")
            .synchronous_execution
            .as_ref()
            .and_then(|execution| execution.heap_frontier.as_ref())
            .expect("Map heap frontier")
            .own_at_init
            .opaque
    );
}

#[test]
fn poisoned_map_adder_rejects_the_interleaved_split() {
    let source = format!(
        "Map.prototype.set = function (key, value) {{ globalThis.touched = true; return this; }};\n{SOURCE}"
    );
    let rejected = expect_cycle_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("odd", &[Member::new("a"), Member::new("c")]),
                logical_module("even", &[Member::new("b"), Member::new("d")]),
            ],
        ),
        &["odd", "even"],
    );
    assert!(
        rejected["cut"]
            .as_array()
            .is_some_and(|cut| !cut.is_empty()),
        "expected a concrete cyclic ordering cut: {rejected:#}"
    );
}

#[test]
fn unsafe_map_constructors_keep_the_opaque_heap_path() {
    let cases = [
        (
            "reassigned global Map",
            "Map = function () { globalThis.touched = true; return new globalThis.Map(); }; const a = new Map([[\"k\", \"v\"]]); export { a };",
        ),
        (
            "global Map getter",
            "Object.defineProperty(globalThis, 'Map', { get() { globalThis.touched = true; return globalThis.savedMap; } }); const a = new Map([[\"k\", \"v\"]]); export { a };",
        ),
        (
            "Map adder replacement",
            "Map.prototype.set = function () { globalThis.touched = true; return this; }; const a = new Map([[\"k\", \"v\"]]); export { a };",
        ),
        (
            "Map adder getter",
            "Object.defineProperty(Map.prototype, 'set', { get() { globalThis.touched = true; return function () {}; } }); const a = new Map([[\"k\", \"v\"]]); export { a };",
        ),
        (
            "Array iterator replacement",
            "Array.prototype[Symbol.iterator] = function* () { globalThis.touched = true; yield ['changed', 'value']; }; const a = new Map([[\"k\", \"v\"]]); export { a };",
        ),
        (
            "Array iterator getter",
            "Object.defineProperty(Array.prototype, Symbol.iterator, { get() { globalThis.touched = true; return function* () {}; } }); const a = new Map([[\"k\", \"v\"]]); export { a };",
        ),
        (
            "Array iterator next replacement",
            "Object.getPrototypeOf([][Symbol.iterator]()).next = function () { globalThis.touched = true; return { done: true }; }; const a = new Map([[\"k\", \"v\"]]); export { a };",
        ),
        (
            "entry getter",
            "const a = new Map([{ get 0() { globalThis.touched = true; return 'k'; }, 1: 'v' }]); export { a };",
        ),
        (
            "sparse entry",
            "const a = new Map([[, 'v']]); export { a };",
        ),
        (
            "spread entry",
            "const a = new Map([[...'kv']]); export { a };",
        ),
        (
            "escaped entries",
            "const entries = [['k', 'v']]; globalThis.saved = entries; const a = new Map(entries); export { a };",
        ),
        (
            "custom iterator",
            "const entries = { [Symbol.iterator]() { globalThis.touched = true; return [][Symbol.iterator](); } }; const a = new Map(entries); export { a };",
        ),
        (
            "object entry value",
            "const a = new Map([['k', { value: 'v' }]]); export { a };",
        ),
        (
            "shadowed Map",
            "function Map() { globalThis.touched = true; return new globalThis.Map(); } const a = new Map([['k', 'v']]); export { a };",
        ),
    ];
    for (label, source) in cases {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        let graph = fixture.dependency_graph();
        let node = node_declaring(&graph, "a");
        let frontier = node
            .synchronous_execution
            .as_ref()
            .and_then(|execution| execution.heap_frontier.as_ref())
            .expect("Map heap frontier");
        assert!(frontier.own_at_init.opaque, "{label}: {node:#?}");
    }
}

#[test]
fn poisoned_native_steps_remain_observable_at_runtime() {
    let cases = [
        "const NativeMap = Map; let touched = 0; Object.defineProperty(globalThis, 'Map', { configurable: true, get() { touched++; return NativeMap; } }); const a = new Map([['k', 'v']]); console.log(touched > 0, a.get('k')); export { a };",
        "const nativeSet = Map.prototype.set; let touched = 0; Map.prototype.set = function (...args) { touched++; return nativeSet.apply(this, args); }; const a = new Map([['k', 'v']]); console.log(touched > 0, a.get('k')); export { a };",
        "const nativeIterator = Array.prototype[Symbol.iterator]; let touched = 0; Array.prototype[Symbol.iterator] = function (...args) { touched++; return nativeIterator.apply(this, args); }; const a = new Map([['k', 'v']]); console.log(touched > 0, a.get('k')); export { a };",
        "const iteratorPrototype = Object.getPrototypeOf([][Symbol.iterator]()); const nativeNext = iteratorPrototype.next; let touched = 0; iteratorPrototype.next = function (...args) { touched++; return nativeNext.apply(this, args); }; const a = new Map([['k', 'v']]); console.log(touched > 0, a.get('k')); export { a };",
    ];
    for source in cases {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        assert_preserved_entry_output(&fixture, "true v\n");
        assert_eq!(fixture.translation_validation()["accepts"], true);
        assert_all_emitted_js_checks(&fixture);
        let graph = fixture.dependency_graph();
        let node = node_declaring(&graph, "a");
        let frontier = node
            .synchronous_execution
            .as_ref()
            .and_then(|execution| execution.heap_frontier.as_ref())
            .expect("Map heap frontier");
        assert!(frontier.own_at_init.opaque, "{node:#?}");
    }
}
