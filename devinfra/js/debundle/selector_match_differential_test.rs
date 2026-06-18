//! Differential parity: the fact-based matcher (`selector_match::matches`, over
//! the `chunk_facts` EDB) must AGREE with the production matcher
//! (`source_match::needle_matches`, the hand-rolled `AstWildcardMatcher`) on the
//! faithful subset, and be fail-closed (`Unsupported`) outside it. This is the
//! P2 parity gauge in miniature — proven, not asserted — over controlled
//! (selector, subject) pairs; the corpus-wide differential is the eventual gate.

use std::collections::BTreeSet;

use spec::{AnonymousStatementSelector, SourceMatchIdentifierMode};

fn exact_selector(source: &str) -> AnonymousStatementSelector {
    AnonymousStatementSelector {
        match_source: source.to_string(),
        identifiers: SourceMatchIdentifierMode::Exact,
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
    expected: bool,
}

#[test]
fn fact_matcher_agrees_with_production_on_faithful_subset() {
    js_ast::with_swc_globals(|| {
        let cases = [
            Case {
                selector: "const a = foo.bar(\"x\");",
                subject: "const a = foo.bar(\"x\");",
                expected: true,
            },
            Case {
                // expression-position single-node hole matches any one subtree
                selector: "const a = foo.bar(EXPR);",
                subject: "const a = foo.bar(\"x\");",
                expected: true,
            },
            Case {
                selector: "const a = foo.bar(\"x\");",
                subject: "const a = foo.bar(\"y\");",
                expected: false,
            },
            Case {
                selector: "const a = foo.baz(\"x\");",
                subject: "const a = foo.bar(\"x\");",
                expected: false,
            },
            Case {
                selector: "const a = b + c;",
                subject: "const a = b + c;",
                expected: true,
            },
            Case {
                selector: "const a = b + c;",
                subject: "const a = b - c;",
                expected: false,
            },
            Case {
                // exact-identifier mode: a different binding name does not match
                selector: "const a = foo(EXPR);",
                subject: "const z = foo(1);",
                expected: false,
            },
        ];
        for case in cases {
            let selector = exact_selector(case.selector);
            let production = source_match::needle_matches(&selector, &first_item(case.subject));
            let fact = selector_match::matches(&facts(case.selector), &facts(case.subject))
                .expect("case is within the faithful subset");
            assert_eq!(
                fact, production,
                "fact matcher and production matcher disagree on {:?} vs {:?}",
                case.selector, case.subject,
            );
            assert_eq!(
                fact, case.expected,
                "unexpected result for {:?} vs {:?}",
                case.selector, case.subject,
            );
        }
    });
}

#[test]
fn fail_closed_on_run_holes() {
    js_ast::with_swc_globals(|| {
        // `ARGS` is a variable-length run hole — not faithful in rung 1, so the
        // fact matcher errors rather than under-constraining the match.
        let result = selector_match::matches(
            &facts("const a = foo.bar(ARGS);"),
            &facts("const a = foo.bar(1, 2);"),
        );
        assert!(
            matches!(result, Err(selector_match::Unsupported { .. })),
            "run hole must be fail-closed, got {result:?}",
        );
    });
}
