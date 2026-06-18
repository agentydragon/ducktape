//! Differential parity: the fact-based matcher (`selector_match::matches`, over
//! the `chunk_facts` EDB) must AGREE with the production matcher
//! (`source_match::needle_matches`, the hand-rolled `AstWildcardMatcher`) on the
//! faithful subset — exact and alpha identifier modes, expression-position
//! single-node holes — and be fail-closed (`Unsupported`) outside it. Proven,
//! not asserted, over controlled (selector, subject) pairs; the corpus-wide
//! differential is the eventual gate.

use std::collections::BTreeSet;

use selector_match::Mode;
use spec::{AnonymousStatementSelector, SourceMatchIdentifierMode};

fn selector(source: &str, alpha: bool) -> AnonymousStatementSelector {
    AnonymousStatementSelector {
        match_source: source.to_string(),
        identifiers: if alpha {
            SourceMatchIdentifierMode::AlphaAll
        } else {
            SourceMatchIdentifierMode::Exact
        },
        target_binding: None,
        target_statement: None,
        target_statements: None,
        wildcard_string_literals: BTreeSet::new(),
    }
}

fn facts(source: &str) -> chunk_facts::ChunkFacts {
    chunk_facts::extract_facts(&js_ast::parse_js_module_ast("<t>", source).unwrap()).unwrap()
}

fn first_item(source: &str) -> swc_ecma_ast::ModuleItem {
    js_ast::parse_js_module_ast("<t>", source)
        .unwrap()
        .body
        .into_iter()
        .next()
        .unwrap()
}

struct Case {
    selector: &'static str,
    subject: &'static str,
    alpha: bool,
    expected: bool,
}

#[test]
fn fact_matcher_agrees_with_production_on_faithful_subset() {
    js_ast::with_swc_globals(|| {
        let cases = [
            // --- exact-identifier mode ---
            Case {
                selector: "const a = foo.bar(\"x\");",
                subject: "const a = foo.bar(\"x\");",
                alpha: false,
                expected: true,
            },
            // expression-position single-node hole matches any one subtree
            Case {
                selector: "const a = foo.bar(EXPR);",
                subject: "const a = foo.bar(\"x\");",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "const a = foo.bar(\"x\");",
                subject: "const a = foo.bar(\"y\");",
                alpha: false,
                expected: false,
            },
            Case {
                selector: "const a = foo.baz(\"x\");",
                subject: "const a = foo.bar(\"x\");",
                alpha: false,
                expected: false,
            },
            Case {
                selector: "const a = b + c;",
                subject: "const a = b + c;",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "const a = b + c;",
                subject: "const a = b - c;",
                alpha: false,
                expected: false,
            },
            // exact mode: a different binding name does not match
            Case {
                selector: "const a = foo(EXPR);",
                subject: "const z = foo(1);",
                alpha: false,
                expected: false,
            },
            // --- alpha-identifier mode (value/binding idents renamable) ---
            Case {
                selector: "function f(a) { return a; }",
                subject: "function g(x) { return x; }",
                alpha: true,
                expected: true,
            },
            // the returned identifier must be the same binding as the param
            Case {
                selector: "function f(a) { return a; }",
                subject: "function g(x) { return y; }",
                alpha: true,
                expected: false,
            },
            Case {
                selector: "const a = b;",
                subject: "const x = y;",
                alpha: true,
                expected: true,
            },
            // injectivity: distinct needle idents cannot both bind to `x`
            Case {
                selector: "const a = b;",
                subject: "const x = x;",
                alpha: true,
                expected: false,
            },
            // prop names are not identifiers — exact even in alpha mode
            Case {
                selector: "const a = o.bar;",
                subject: "const z = q.bar;",
                alpha: true,
                expected: true,
            },
            Case {
                selector: "const a = o.bar;",
                subject: "const z = q.baz;",
                alpha: true,
                expected: false,
            },
        ];
        for case in cases {
            let selector = selector(case.selector, case.alpha);
            let mode = if case.alpha {
                Mode::AlphaAll
            } else {
                Mode::Exact
            };
            let production = source_match::needle_matches(&selector, &first_item(case.subject));
            let fact = selector_match::matches(&facts(case.selector), &facts(case.subject), mode)
                .expect("case is within the faithful subset");
            assert_eq!(
                fact, production,
                "fact matcher and production matcher disagree on {:?} vs {:?} (alpha={})",
                case.selector, case.subject, case.alpha,
            );
            assert_eq!(
                fact, case.expected,
                "unexpected result for {:?} vs {:?} (alpha={})",
                case.selector, case.subject, case.alpha,
            );
        }
    });
}

#[test]
fn fail_closed_on_run_holes() {
    js_ast::with_swc_globals(|| {
        // `ARGS` is a variable-length run hole — not faithful yet, so the fact
        // matcher errors rather than under-constraining the match.
        let result = selector_match::matches(
            &facts("const a = foo.bar(ARGS);"),
            &facts("const a = foo.bar(1, 2);"),
            Mode::Exact,
        );
        assert!(
            matches!(result, Err(selector_match::Unsupported { .. })),
            "run hole must be fail-closed, got {result:?}",
        );
    });
}
