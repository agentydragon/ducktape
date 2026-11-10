//! Each fixture is one statement (or one split declaration) whose effect on
//! `state` Node observes when it is swapped with the statement that reads
//! `state`. Both analyses must report a conflict for every one of them.

use summary_effects::generate::FunctionHeap;
use summary_effects::report::analyze;
use summary_effects::tier0;

const HEADER: &str =
    "let state = 0;\nlet holder;\nfunction noop() {}\nfunction second() { state = 1; }\n";
const HEADER_UNITS: usize = 4;
const ALL_UPGRADES: [tier0::Upgrade; 2] = [
    tier0::Upgrade::PrimitiveBindings,
    tier0::Upgrade::NativeAllocators,
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
        ] {
            assert!(!last_is_pure(impure), "{impure}");
        }
    });
}
