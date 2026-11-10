//! Each fixture is one statement (or one split declaration) whose effect on
//! `state` Node observes when it is swapped with the statement that reads
//! `state`. Both analyses must report a conflict for every one of them.

use summary_effects::generate::FunctionHeap;
use summary_effects::report::analyze;
use summary_effects::tier0;

const HEADER: &str =
    "let state = 0;\nlet holder;\nfunction noop() {}\nfunction second() { state = 1; }\n";
const HEADER_UNITS: usize = 4;
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
        for exports_reachable in [false, true] {
            let accepted = accepted(|name, source| {
                let parsed = js_ast::parse_js_module(name, source).expect("the fixture parses");
                let unresolved =
                    swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
                let analysis = tier0::analyze(
                    &parsed.module,
                    unresolved,
                    &tier0::Options { exports_reachable },
                );
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
