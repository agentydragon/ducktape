//! Each fixture is one statement (or one split declaration) whose effect on
//! `state` Node observes when it is swapped with the statement that reads
//! `state`. Both analyses must report a conflict for every one of them.

use summary_effects::generate::FunctionHeap;
use summary_effects::report::analyze;
use summary_effects::tier0;

const HEADER: &str =
    "let state = 0;\nlet holder;\nfunction noop() {}\nfunction second() { state = 1; }\n";
const HEADER_UNITS: usize = 4;
const ALL_UPGRADES: [tier0::Upgrade; 4] = [
    tier0::Upgrade::PrimitiveBindings,
    tier0::Upgrade::NativeAllocators,
    tier0::Upgrade::IntrinsicHeritage,
    tier0::Upgrade::IntrinsicMemberReads,
];
const FIXTURES: [&str; 2] = [
    include_str!("soundness_fixtures.txt"),
    include_str!("definition_fixtures.txt"),
];

/// Whole programs: a prelude line, the two swapped statements, and a line
/// that prints the observation.
const REACH_FIXTURES: &str = include_str!("reach_fixtures.txt");

/// Each program with the ordinals of the fixture units and of the unit they
/// are swapped with, counted from the end.
fn programs() -> impl Iterator<Item = (&'static str, String, usize)> {
    let headed = FIXTURES.into_iter().flat_map(|fixtures| {
        fixtures.split("### ").skip(1).map(|block| {
            let (name, statement) = block.split_once('\n').expect("a name line");
            let source = format!("{HEADER}{}\nconst seen = state;\n", statement.trim());
            (name, source, 1)
        })
    });
    let whole = REACH_FIXTURES.split("### ").skip(1).map(|block| {
        let (name, program) = block.split_once('\n').expect("a name line");
        (name, program.to_string(), 2)
    });
    headed.chain(whole)
}

/// The fixtures for which no fixture unit conflicts with the final read.
fn accepted(
    conflicts: impl Fn(&str, &str) -> (usize, Box<dyn Fn(usize, usize) -> bool>),
) -> Vec<&'static str> {
    programs()
        .filter_map(|(name, source, from_end)| {
            let (units, conflict) = conflicts(name, &source);
            assert!(
                units > from_end + 1,
                "{name}: the fixture has two statements to swap"
            );
            let read = units - from_end;
            // A headed fixture may be a split declaration; a whole program
            // swaps exactly the unit before the read.
            let first = if from_end == 1 {
                HEADER_UNITS
            } else {
                read - 1
            };
            let refused = (first..read).any(|unit| conflict(unit, read));
            (!refused).then_some(name)
        })
        .collect()
}

#[test]
fn points_to_refuses_every_divergent_swap() {
    js_ast::with_swc_globals(|| {
        for function_heap in [
            FunctionHeap::Site,
            FunctionHeap::Function,
            FunctionHeap::Global,
        ] {
            let accepted = accepted(|name, source| {
                let analysis = analyze(name, source, function_heap).expect("the fixture parses");
                let units = analysis.check.n;
                (
                    units,
                    Box::new(move |a, b| analysis.check.flags(a, b, 0) != 0),
                )
            });
            assert_eq!(accepted, Vec::<&str>::new(), "{function_heap:?}");
        }
    });
}

#[test]
fn tier0_refuses_every_divergent_swap() {
    js_ast::with_swc_globals(|| {
        for (exports_reachable, upgrades) in [
            (false, Vec::new()),
            (true, Vec::new()),
            (false, ALL_UPGRADES.to_vec()),
        ] {
            let options = tier0::Options {
                exports_reachable,
                upgrades: upgrades.into_iter().collect(),
                what_if_pure: Default::default(),
                program: None,
                premises: Default::default(),
                derive: false,
            };
            let accepted = accepted(|name, source| {
                let parsed = js_ast::parse_js_module(name, source).expect("the fixture parses");
                let unresolved =
                    swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
                let analysis = tier0::analyze(&parsed.module, unresolved, &options);
                let units = analysis.units.len();
                (
                    units,
                    Box::new(move |a, b| analysis.conflict(a, b).is_some()),
                )
            });
            assert_eq!(accepted, Vec::<&str>::new(), "{exports_reachable}");
        }
    });
}

/// Whether tier 0, with every upgrade, calls the last statement of `source`
/// a pure definition.
fn last_is_pure(source: &str) -> bool {
    let parsed = js_ast::parse_js_module("classification", source).expect("the source parses");
    let unresolved = swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
    let options = tier0::Options {
        exports_reachable: false,
        upgrades: ALL_UPGRADES.into_iter().collect(),
        what_if_pure: Default::default(),
        program: None,
        premises: Default::default(),
        derive: false,
    };
    let analysis = tier0::analyze(&parsed.module, unresolved, &options);
    analysis.units.last().expect("a statement").kind == tier0::Kind::Pure
}

#[test]
fn tier0_upgrades_stop_at_their_near_misses() {
    js_ast::with_swc_globals(|| {
        // Operators over bindings that only ever hold a primitive.
        for pure in [
            "const a = 1; const b = 2 * a + 1;",
            "const a = 'x'; const b = `${a}-${a}`;",
            "const a = 1, b = a < 2 ? a : -a; const c = a + b;",
            "let a; const b = a + 1;",
        ] {
            assert!(last_is_pure(pure), "{pure}");
        }
        for impure in [
            // The operand may be an object, whose conversion runs code.
            "const a = { valueOf() { return 1; } }; const b = 2 * a;",
            "let a = 1; a = { valueOf() { return 1; } }; const b = 2 * a;",
            "let a = 1; function set(v) { a = v; } const b = 2 * a;",
            "var a = 1; var a = {}; const b = 2 * a;",
            "import { a } from './other.js'; const b = 2 * a;",
            "const b = 2 * someGlobal;",
            "const a = 1n; const b = a + 1;",
            "const a = 1; const b = a in {};",
            "const a = [1]; const b = `${a}`;",
            "const a = 1; const b = a.toFixed(2);",
        ] {
            assert!(!last_is_pure(impure), "{impure}");
        }
        // Allocations by listed intrinsics over arguments that run no code.
        for pure in [
            "const a = new Map();",
            "const a = new WeakMap();",
            "const a = new Set([1, 'x']);",
            "const f = () => 1; const a = new Map([['k', f], [2, { v: 1 }]]);",
            "const a = new Uint8Array([0, 1, 2]);",
            "const a = new Float64Array(16);",
            "const a = Object.freeze({ x: 1, y: [2] });",
            "const a = Object.create(null);",
            "const a = Symbol('tag');",
            "const a = Symbol.for('tag');",
        ] {
            assert!(last_is_pure(pure), "{pure}");
        }
        for impure in [
            // An iterable that is not a fresh literal.
            "const x = []; const a = new Map(x);",
            "const x = [1]; const a = new Set(x);",
            "const a = new Set([...'ab']);",
            // A key or description whose conversion runs code.
            "const k = {}; const a = new Map([[k, 1]]);",
            "const o = { toString() { return 'x'; } }; const a = Symbol(o);",
            // A length that may throw.
            "const n = 4; const a = new Uint8Array(n);",
            "const a = new Uint8Array(-1);",
            "const a = new Uint8Array(1e12);",
            // An object that already exists.
            "const o = {}; const a = Object.freeze(o);",
            "const p = {}; const a = Object.create(p);",
            // A local binding shadowing the intrinsic.
            "class Map {} const a = new Map();",
            "const a = new Promise(() => {});",
            "const a = new RegExp('x');",
            "const a = Object.freeze({ get x() { return 1; }, ...{} });",
            // The chunk modifies what the allocation depends on.
            "Map.prototype.set = function () {}; const a = new Map([['k', 1]]);",
            "Array.prototype[Symbol.iterator] = function* () {}; const a = new Set([1]);",
            "globalThis.Map = class {}; const a = new Map();",
            // A store through an alias the census follows.
            "const proto = Set.prototype; proto.add = function () {}; const a = new Set([1]);",
            "const S = Set; S.prototype.add = function () {}; const a = new Set([1]);",
            "let S; S = typeof Set === 'function' ? Set : null; S.prototype.add = 1; const a = new Set([1]);",
            "function patch(target) { target.prototype.set = 1; } patch(Map); const a = new Map([['k', 1]]);",
            "const patch = (target, name = 'set') => { target[name] = 1; }; patch(Map.prototype); const a = new Map([['k', 1]]);",
            "const root = typeof globalThis === 'object' ? globalThis : window; root.Map = class {}; const a = new Map();",
            "const root = globalThis; root[k] = 1; const a = new Map();",
            "(function (root) { root.Map = class {}; })(globalThis); const a = new Map();",
            "((root, name) => { root[name] = 1; })(window, 'Map'); const a = new Map();",
            // A store through `.prototype` of something an escaped
            // constructor may have reached.
            "[Map, Set].forEach((C) => { C.prototype.set = 1; }); const a = new Map([['k', 1]]);",
            "const known = { Map }; for (const k in known) known[k].prototype[k] = 1; const a = new Map();",
            "export const M = Map; function f(C) { C.prototype.set = 1; } const a = new Map([['k', 1]]);",
            "function patch(target) { target.prototype.set = 1; } patch(Set); [Map].forEach(patch); const a = new Map([['k', 1]]);",
            "Object.defineProperty(Object, 'freeze', { value() {} }); const a = Object.freeze({});",
        ] {
            assert!(!last_is_pure(impure), "{impure}");
        }
        // Using an intrinsic as a value does not modify it, nor does a
        // write to an unrelated property.
        for pure in [
            "const proto = Set.prototype; const a = new Set([1]);",
            "window[Symbol.for('tag')] = true; const a = new Map();",
            "function listen(target) { target.onload = null; } listen(window); const a = new Map();",
            "function check(target) { return typeof target; } check(Map); const a = new Map();",
            "const supported = [Map, Set].every(Boolean); const a = new Map();",
            "const M = typeof Map === 'function' ? Map : null; const a = new Map();",
            "const S = Set; S.prototype.extra = 1; const a = new Set([1]);",
            "[Map, Set].forEach((C) => { C.prototype.extra = 1; }); const a = new Map([['k', 1]]);",
            "function f(o) { o.prototype = {}; o.set = 1; } const known = [Map]; const a = new Map([['k', 1]]);",
            "Array.prototype.at = function () {}; const a = new Set([1]);",
            "Object.defineProperty(Array.prototype, 'at', { value() {} }); const a = new Set([1]);",
            "Error.stackTraceLimit = 50; const a = class extends Error {};",
        ] {
            assert!(last_is_pure(pure), "{pure}");
        }
        // Classes extending an intrinsic constructor.
        for pure in [
            "class A extends Error { constructor() { super('x'); } }",
            "const A = class extends TypeError {};",
            "class A extends Array {}",
        ] {
            assert!(last_is_pure(pure), "{pure}");
        }
        for impure in [
            "class B {} class A extends B {}",
            "class A extends DOMException {}",
            "class A extends (0, Error) {}",
            "globalThis.Error = class {}; class A extends Error {}",
            "Error = class {}; class A extends Error {}",
            "class A extends Error { static x = 1; }",
        ] {
            assert!(!last_is_pure(impure), "{impure}");
        }
        // Reads of intrinsic data properties.
        for pure in [
            "const a = Object.prototype.toString;",
            "const a = Math.PI;",
            "const a = Object.assign;",
            "const a = Symbol.iterator;",
            "const a = { has: Object.prototype.hasOwnProperty, max: Number.MAX_SAFE_INTEGER };",
        ] {
            assert!(last_is_pure(pure), "{pure}");
        }
        for impure in [
            // The prototype object itself, an accessor, a deeper path.
            "const a = Array.prototype;",
            "const a = Object.prototype.__proto__;",
            "const a = Function.prototype.caller;",
            "const a = Map.prototype.size;",
            "const a = RegExp.lastMatch;",
            "const a = Object.prototype.toString.call;",
            "const a = navigator.platform;",
            "const o = {}; const a = o.x;",
            // The chunk writes the property read.
            "Object.assign = () => {}; const a = Object.assign;",
            "Object.prototype.toString = () => ''; const a = Object.prototype.toString;",
            "Math[k] = 1; const a = Math.PI;",
            "window[k] = 1; const a = Math.PI;",
        ] {
            assert!(!last_is_pure(impure), "{impure}");
        }
    });
}

fn tier0(source: &str, exports_reachable: bool) -> tier0::Tier0 {
    let parsed = js_ast::parse_js_module("fixture", source).expect("the source parses");
    let unresolved = swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
    let options = tier0::Options {
        exports_reachable,
        upgrades: ALL_UPGRADES.into_iter().collect(),
        what_if_pure: Default::default(),
        program: None,
        premises: Default::default(),
        derive: false,
    };
    tier0::analyze(&parsed.module, unresolved, &options)
}

/// Another chunk in an import cycle with this one can call this chunk's
/// exports while this chunk initializes. Tier 0 sees that only when told.
#[test]
fn tier0_export_cycle_needs_the_precondition() {
    js_ast::with_swc_globals(|| {
        let source = "import { run } from './other.js';\n\
            export function probe() { return later; }\n\
            run();\n\
            var later = 1;\n";
        // Unit 2 calls into the other chunk; unit 3 defines what `probe` reads.
        assert_eq!(tier0(source, false).conflict(2, 3), None);
        assert_eq!(
            tier0(source, true).conflict(2, 3),
            Some(tier0::Conflict::ReachEscaped)
        );
    });
}

/// Compiler output shapes that dominate a large private consumer corpus,
/// with the verdict tier 0 gives each pair.
#[test]
fn tier0_synthetic_shape_families() {
    js_ast::with_swc_globals(|| {
        use tier0::Conflict::{BothImpure, ReachEscaped, ReachOwn};

        // The TypeScript decorator helper over two classes.
        let decorators = tier0(
            "import { imported } from './dep.js';\n\
             var decorate = function (decorators, target, key, desc) {\n\
               var r = desc;\n\
               for (var i = decorators.length - 1; i >= 0; i--) r = decorators[i](target, key, r) || r;\n\
               return r && Object.defineProperty(target, key, r), r;\n\
             };\n\
             function local(target, key) {}\n\
             class A { m() {} }\n\
             class B { m() {} }\n\
             decorate([local], A.prototype, 'm', null);\n\
             decorate([imported], B.prototype, 'm', null);\n\
             var unrelated = { n: 1 };\n",
            false,
        );
        let (class_a, class_b, on_a, on_b, unrelated) = (3, 4, 5, 6, 7);
        // Decorator statements keep their mutual order.
        assert_eq!(decorators.conflict(on_a, on_b), Some(BothImpure));
        // Each stays after its own class.
        assert_eq!(decorators.conflict(class_a, on_a), Some(ReachOwn));
        assert_eq!(decorators.conflict(class_b, on_b), Some(ReachOwn));
        // The first may cross the other class; the second may not cross the
        // class the first one leaked.
        assert_eq!(decorators.conflict(class_b, on_a), None);
        assert_eq!(decorators.conflict(class_a, on_b), Some(ReachEscaped));
        // An unrelated definition moves freely.
        assert_eq!(decorators.conflict(on_a, unrelated), None);
        assert_eq!(decorators.conflict(on_b, unrelated), None);

        // A local factory called with an object literal.
        let factories = tier0(
            "var registry = [];\n\
             function make(options) { registry.push(options); return { ...options }; }\n\
             var first = make({ a: 1 });\n\
             var second = make({ b: 2 });\n\
             var unrelated = () => 3;\n\
             var table = { first: 1 };\n",
            false,
        );
        let (registry, first, second, unrelated, table) = (0, 2, 3, 4, 5);
        assert_eq!(factories.conflict(first, second), Some(BothImpure));
        // The factory's registry stays ahead of every call.
        assert_eq!(factories.conflict(registry, first), Some(ReachOwn));
        assert_eq!(factories.conflict(first, unrelated), None);
        assert_eq!(factories.conflict(second, table), None);

        // A wrapper called with a function expression.
        let wrappers = tier0(
            "import { wrap } from './dep.js';\n\
             var Component = wrap(function () { return dependency; });\n\
             var dependency = 1;\n\
             var other = wrap(() => 2);\n\
             var unrelated = 'x';\n",
            false,
        );
        let (component, dependency, other, unrelated) = (1, 2, 3, 4);
        // The wrapper may call the function at once.
        assert_eq!(wrappers.conflict(component, dependency), Some(ReachOwn));
        assert_eq!(wrappers.conflict(component, other), Some(BothImpure));
        assert_eq!(wrappers.conflict(component, unrelated), None);
        assert_eq!(wrappers.conflict(other, unrelated), None);
    });
}

/// Programs with premises, as `### <verdict> <node>: <name>`, the premises,
/// a prelude line, the two swapped statements and a line that prints. The
/// first block is the module the programs import.
const PREMISE_FIXTURES: &str = include_str!("premise_fixtures.txt");

/// Premises decide exactly the fixtures marked `commutes`; where Node then
/// disagrees (`commutes differs`) the premise is false, which tier 0 does
/// not check.
#[test]
fn tier0_premises_are_trusted_and_bounded() {
    js_ast::with_swc_globals(|| {
        for block in PREMISE_FIXTURES.split("### ").skip(2) {
            let (head, rest) = block.split_once('\n').expect("a head line");
            let (premises, source) = rest.split_once('\n').expect("a premises line");
            let conflict = |premises: &str| {
                let parsed = js_ast::parse_js_module("fixture", source).expect("it parses");
                let unresolved =
                    swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
                let options = tier0::Options {
                    exports_reachable: false,
                    upgrades: ALL_UPGRADES.into_iter().collect(),
                    what_if_pure: Default::default(),
                    program: None,
                    premises: serde_json::from_str(premises).expect("the premises parse"),
                    derive: false,
                };
                let analysis = tier0::analyze(&parsed.module, unresolved, &options);
                let units = analysis.units.len();
                assert_eq!(analysis.premises_matched, analysis.premises_given, "{head}");
                analysis.conflict(units - 3, units - 2)
            };
            assert!(conflict("{}").is_some(), "{head}: refused without premises");
            assert_eq!(
                conflict(premises).is_none(),
                head.starts_with("commutes"),
                "{head}"
            );
        }
    });
}

/// Programs importing `./dep.js`, the first block, in the layout of the
/// premise fixtures; the second line of each is unused.
const DERIVE_FIXTURES: &str = include_str!("derive_fixtures.txt");

/// Derivation from bodies decides exactly the fixtures marked `commutes`.
#[test]
fn tier0_derivation_matches_node() {
    let directory = std::env::temp_dir().join(format!("summary_effects_{}", std::process::id()));
    std::fs::create_dir_all(&directory).expect("a scratch directory");
    let mut blocks = DERIVE_FIXTURES.split("### ").skip(1);
    let (_, dep) = blocks
        .next()
        .and_then(|block| block.split_once('\n'))
        .expect("a dep block");
    std::fs::write(directory.join("dep.js"), dep).expect("writing the provider");
    let chunk = directory.join("main.js");
    js_ast::with_swc_globals(|| {
        for block in blocks {
            let (head, rest) = block.split_once('\n').expect("a head line");
            let (_, source) = rest.split_once('\n').expect("an unused line");
            std::fs::write(&chunk, source).expect("writing the chunk");
            let conflict = |derive: bool| {
                let parsed = js_ast::parse_js_module("fixture", source).expect("it parses");
                let unresolved =
                    swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
                let options = tier0::Options {
                    exports_reachable: false,
                    upgrades: ALL_UPGRADES.into_iter().collect(),
                    what_if_pure: Default::default(),
                    program: Some(
                        summary_effects::program::load(&directory, &chunk, None)
                            .expect("the program loads"),
                    ),
                    premises: Default::default(),
                    derive,
                };
                let analysis = tier0::analyze(&parsed.module, unresolved, &options);
                let units = analysis.units.len();
                analysis.conflict(units - 3, units - 2)
            };
            assert!(
                conflict(false).is_some(),
                "{head}: refused without derivation"
            );
            assert_eq!(
                conflict(true).is_none(),
                head.starts_with("commutes"),
                "{head}"
            );
        }
    });
    std::fs::remove_dir_all(&directory).expect("removing the scratch directory");
}

/// Whether derivation alone makes the last statement of `source` a pure
/// definition.
fn last_is_derived_pure(source: &str) -> bool {
    let parsed = js_ast::parse_js_module("classification", source).expect("the source parses");
    let unresolved = swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
    let options = tier0::Options {
        exports_reachable: false,
        upgrades: ALL_UPGRADES.into_iter().collect(),
        what_if_pure: Default::default(),
        program: None,
        premises: Default::default(),
        derive: true,
    };
    let analysis = tier0::analyze(&parsed.module, unresolved, &options);
    analysis.units.last().expect("a statement").kind == tier0::Kind::Pure
}

#[test]
fn tier0_allocators_stop_at_their_near_misses() {
    js_ast::with_swc_globals(|| {
        for allocator in [
            "function make(a) { return { a }; } const x = make(1);",
            "const wrap = (f) => ({ f, list: [f] }); const x = wrap(() => 1);",
            "function ctx(v) { return v = { current: v }, v.self = v, v; } const x = ctx(1);",
            "function pair(a, b) { var o = { a }; o.b = b; return o; } const x = pair(1, 2);",
            "function outer(a) { return inner(a); } function inner(a) { return [a]; } const x = outer(1);",
            "const k = Symbol.for('t'); function tagged(a) { return { k, a }; } const x = tagged(1);",
            "function opt(a, b) { return { a, b: b === void 0 ? null : b }; } const x = opt(1);",
            "const later = (f) => () => f; const x = later(1);",
            // A property read of an argument written as a plain literal.
            "function f(o) { return { v: o.v }; } const x = f({ v: 1 });",
            "function f(t, o) { return { t, k: o.key, o }; } const k = 1; const x = f('a', { key: k });",
            "class Box { constructor(v) { this.v = v; } get() { return this.v; } } const x = new Box(1);",
            "class A { constructor(v) { this.v = v; } } class B extends A { constructor(v) { super(v); this.k = 1; } } const x = new B(1);",
        ] {
            assert!(last_is_derived_pure(allocator), "{allocator}");
        }
        for other in [
            // Reads, calls or writes what it is given.
            "function f(o) { return { v: o.v }; } const o = {}; const x = f(o);",
            "function f(o) { return { v: o.v }; } const x = f({ get v() { return 1; } });",
            "function f(o) { return { v: o.v }; } const a = {}; const x = f({ ...a });",
            "function f(o) { return { v: o.v }; } const x = f();",
            "function f(o) { return { v: o.v.w }; } const x = f({ v: {} });",
            "function f(o) { o = { v: 1 }; return { v: o.v }; } const x = f({});",
            "function f(g) { return { v: g() }; } const x = f(() => 1);",
            "function f(o) { o.v = 1; return o; } const x = f({});",
            "function f(a) { return { ...a }; } const x = f({});",
            "function f(a) { return { [a]: 1 }; } const x = f('k');",
            "function f(a) { return a + 1; } const x = f(1);",
            "function f(a) { return `${a}`; } const x = f(1);",
            "function f(a = {}) { return { a }; } const x = f();",
            "function f({ a }) { return { a }; } const x = f({});",
            // Touches state outside itself.
            "let n = 0; function f() { return { n }; } n = 1; const x = f();",
            "const seen = []; function f(a) { seen.push(a); return {}; } const x = f(1);",
            "const box = {}; function f(a) { box.a = a; return {}; } const x = f(1);",
            "let last; function f(a) { last = a; return {}; } const x = f(1);",
            "function f(a) { return new Thing(a); } const x = f(1);",
            "function f(a) { return other(a); } function other(a) { return a.x; } const x = f({});",
            // Not one plain synchronous body.
            "async function f(a) { return { a }; } const x = f(1);",
            "function* f(a) { return { a }; } const x = f(1);",
            "function f(a) { if (a) return {}; return []; } const x = f(1);",
            "function f(a) { return { get a() { return a; } }; } const x = f(1);",
            // A constructor that does more than store.
            "class A { constructor(v) { this.v = v; log(v); } } const x = new A(1);",
            "class A { set v(x) {} constructor(v) { this.v = v; } } const x = new A(1);",
            "class A { v = make(); } const x = new A();",
            "class B {} class A extends B { constructor() { super(); } } B = class {}; const x = new A();",
            "class A extends Unknown { constructor() { super(); } } const x = new A();",
            // The binding may hold another function.
            "function f(a) { return { a }; } f = (a) => a.x; const x = f(1);",
            "var f = (a) => ({ a }); var f = (a) => a.x; const x = f(1);",
        ] {
            assert!(!last_is_derived_pure(other), "{other}");
        }
    });
}

/// The class derivation alone gives the last statement of `source`.
fn derived_kind(source: &str) -> tier0::Kind {
    let parsed = js_ast::parse_js_module("family", source).expect("the source parses");
    let unresolved = swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
    let options = tier0::Options {
        exports_reachable: false,
        upgrades: ALL_UPGRADES.into_iter().collect(),
        what_if_pure: Default::default(),
        program: None,
        premises: Default::default(),
        derive: true,
    };
    let analysis = tier0::analyze(&parsed.module, unresolved, &options);
    analysis.units.last().expect("a statement").kind
}

/// Callee families of a large private consumer corpus, as synthetic
/// definitions, with the class derivation gives a call of each.
#[test]
fn tier0_derivation_classifies_callee_families() {
    use tier0::Kind::{Contained, Pure};
    let impure = |kind| matches!(kind, tier0::Kind::Impure(_));
    js_ast::with_swc_globals(|| {
        for (family, expected, source) in [
            (
                "context creator",
                Pure,
                "const tag = Symbol.for('context');\n\
                 function createContext(value) {\n\
                   return value = { $$typeof: tag, current: value, Provider: null },\n\
                     value.Provider = value, value;\n\
                 }\n\
                 const Context = createContext(null);",
            ),
            (
                "message-catalog definer",
                Pure,
                "const defineMessages = (messages) => messages;\n\
                 const messages = defineMessages({ title: { id: 'title', text: 'Title' } });",
            ),
            (
                "store creator that registers",
                Contained,
                "const stores = new Map();\n\
                 function defineStore(id, setup) {\n\
                   const store = { id, setup };\n\
                   stores.set(id, store);\n\
                   return store;\n\
                 }\n\
                 const useStore = defineStore('main', () => ({ count: 0 }));",
            ),
            (
                "route or lazy-panel definer",
                Pure,
                "const tag = Symbol.for('lazy');\n\
                 function init(payload) { return payload._result(); }\n\
                 function lazy(load) {\n\
                   return { $$typeof: tag, _payload: { _status: -1, _result: load }, _init: init };\n\
                 }\n\
                 const Panel = lazy(() => import('./panel.js'));",
            ),
            (
                "schema-object builder",
                Pure,
                "class Type { constructor(def) { this._def = def; } parse(value) { return value; } }\n\
                 class ObjectType extends Type {\n\
                   constructor(def) { super(def); this.kind = 'object'; }\n\
                 }\n\
                 const object = (shape) => new ObjectType({ shape: () => shape });\n\
                 const text = () => new Type({ kind: 'text' });\n\
                 const schema = object({ name: text(), inner: object({}) });",
            ),
            (
                "bundler function-naming helper",
                Pure,
                "var defProp = Object.defineProperty;\n\
                 var name = (target, value) => defProp(target, 'name', { value, configurable: true });\n\
                 const handler = name(function () {}, 'handler');",
            ),
            (
                "CommonJS-style module wrapper applied to a function",
                Pure,
                "var commonJS = (cb, mod) => () => (mod || cb((mod = { exports: {} }).exports, mod), mod.exports);\n\
                 var requireThing = commonJS((exports) => { exports.value = 1; });",
            ),
            (
                "memo-style component wrapper",
                Pure,
                "const tag = Symbol.for('memo');\n\
                 function memo(type, compare) {\n\
                   return { $$typeof: tag, type, compare: compare === void 0 ? null : compare };\n\
                 }\n\
                 const Component = memo((props) => props.children);",
            ),
            (
                "forwardRef-style component wrapper",
                Pure,
                "const tag = Symbol.for('forward_ref');\n\
                 const forwardRef = (render) => ({ $$typeof: tag, render });\n\
                 const Component = forwardRef((props, ref) => null);",
            ),
            (
                "lazy-thunk wrapper that registers a reset hook",
                Contained,
                "const resets = [];\n\
                 function once(compute) {\n\
                   let done = false, value;\n\
                   resets.push(() => { done = false; });\n\
                   return () => done ? value : (done = true, value = compute());\n\
                 }\n\
                 const settings = once(() => ({ theme: 'dark' }));",
            ),
            (
                "factory that numbers its results",
                Contained,
                "let next = 0;\n\
                 function atom(init) { return { key: `atom${++next}`, init }; }\n\
                 const count = atom(0);",
            ),
            (
                "element factory over a literal",
                Pure,
                "function element(type, props) { return { type, key: props.key, props }; }\n\
                 const node = element('div', { key: 'a', title: 'A' });",
            ),
        ] {
            assert_eq!(derived_kind(source), expected, "{family}");
        }
        // Families derivation leaves impure: a premise is the only way.
        for (family, source) in [
            (
                "store creator that calls its setup",
                "function create(setup) { const state = setup(); return { get: () => state }; }\n\
                 const store = create(() => ({ count: 0 }));",
            ),
            (
                "function-naming helper on an existing function",
                "var defProp = Object.defineProperty;\n\
                 var name = (target, value) => defProp(target, 'name', { value, configurable: true });\n\
                 function existing() {}\n\
                 const handler = name(existing, 'handler');",
            ),
            (
                "element factory that copies its argument",
                "function element(type, config) {\n\
                   var props = {};\n\
                   for (var key in config) key !== 'key' && (props[key] = config[key]);\n\
                   return { type, props };\n\
                 }\n\
                 const node = element('div', { title: 'A' });",
            ),
            (
                "registering factory whose list is reassigned elsewhere",
                "let items = [];\n\
                 function register(item) { items.push(item); return item; }\n\
                 function replace(next) { items = next; }\n\
                 const first = register({ id: 1 });",
            ),
        ] {
            assert!(impure(derived_kind(source)), "{family}");
        }
    });
}
