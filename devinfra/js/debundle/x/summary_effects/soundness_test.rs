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
            "const proto = Set.prototype; const a = new Set([1]);",
            "function patch(target) {} patch(Map); const a = new Map();",
            "Object.defineProperty(Object, 'freeze', { value() {} }); const a = Object.freeze({});",
        ] {
            assert!(!last_is_pure(impure), "{impure}");
        }
        // A write to an unrelated property leaves the allocation pure.
        for pure in [
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
            let conflict = |callees: &str| {
                let parsed = js_ast::parse_js_module("fixture", source).expect("it parses");
                let unresolved =
                    swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
                let options = tier0::Options {
                    exports_reachable: false,
                    upgrades: ALL_UPGRADES.into_iter().collect(),
                    what_if_pure: Default::default(),
                    program: None,
                    premises: serde_json::from_str(&format!("{{\"callees\":{callees}}}"))
                        .expect("the premises parse"),
                };
                let analysis = tier0::analyze(&parsed.module, unresolved, &options);
                let units = analysis.units.len();
                assert_eq!(analysis.premises_matched, analysis.premises_given, "{head}");
                analysis.conflict(units - 3, units - 2)
            };
            assert!(conflict("[]").is_some(), "{head}: refused without premises");
            assert_eq!(
                conflict(premises).is_none(),
                head.starts_with("commutes"),
                "{head}"
            );
        }
    });
}
