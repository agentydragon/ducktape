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

/// Per-top-level-statement facts (one `ChunkFacts` per item), for the
/// multi-statement sequence matcher.
fn roots(source: &str) -> Vec<chunk_facts::ChunkFacts> {
    let module = js_ast::parse_js_module_ast("<t>", source).unwrap();
    let span = module.span;
    module
        .body
        .into_iter()
        .map(|item| {
            chunk_facts::extract_facts(&swc_ecma_ast::Module {
                span,
                body: vec![item],
                shebang: None,
            })
            .unwrap()
        })
        .collect()
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
            // --- run holes: ordered subsequence with gaps ---
            // ARGS absorbs the whole argument run (including an empty one).
            Case {
                selector: "const a = foo.bar(ARGS);",
                subject: "const a = foo.bar(1, 2);",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "const a = foo.bar(ARGS);",
                subject: "const a = foo.bar();",
                alpha: false,
                expected: true,
            },
            // anchored-left fixed segment before the hole: first arg must match.
            Case {
                selector: "const a = foo(x, ARGS);",
                subject: "const a = foo(x, 1, 2);",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "const a = foo(x, ARGS);",
                subject: "const a = foo(y, 1);",
                alpha: false,
                expected: false,
            },
            // anchored-right fixed segment after the hole: last arg must match.
            Case {
                selector: "const a = foo(ARGS, z);",
                subject: "const a = foo(1, 2, z);",
                alpha: false,
                expected: true,
            },
            // STMT_LIST absorbs leading statements; the trailing segment is anchored.
            Case {
                selector: "function f() { STMT_LIST; return 1; }",
                subject: "function f() { a(); b(); return 1; }",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "function f() { STMT_LIST; return 1; }",
                subject: "function f() { return 2; }",
                alpha: false,
                expected: false,
            },
            // OBJECT_PROPS absorbs the surrounding properties; a two-hole list
            // with an interior fixed segment (the corpus `{…, k, …}` shape).
            Case {
                selector: "const o = { a: 1, OBJECT_PROPS };",
                subject: "const o = { a: 1, b: 2 };",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "const o = { OBJECT_PROPS, k: 1, OBJECT_PROPS };",
                subject: "const o = { x: 0, k: 1, y: 2 };",
                alpha: false,
                expected: true,
            },
            // run hole under alpha mode: the binding still flows through the
            // fixed segment (`a`↔`x`) while STMT_LIST absorbs the rest.
            Case {
                selector: "function f(a) { STMT_LIST; return a; }",
                subject: "function g(x) { y(); return x; }",
                alpha: true,
                expected: true,
            },
            // --- parse-position-polymorphic single-node holes ---
            // ANYTHING in parameter (pattern) position matches any param and
            // never binds: two ANYTHING params are independent (they would
            // collide if treated as one alpha binding).
            Case {
                selector: "function f(ANYTHING, ANYTHING) { return 1; }",
                subject: "function f(a, b) { return 1; }",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "function f(ANYTHING, ANYTHING) { return ANYTHING; }",
                subject: "function g(a, b) { return c; }",
                alpha: true,
                expected: true,
            },
            // STMT in statement position matches exactly one statement (not a run).
            Case {
                selector: "function f() { STMT; return 1; }",
                subject: "function f() { a(); return 1; }",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "function f() { STMT; return 1; }",
                subject: "function f() { a(); b(); return 1; }",
                alpha: false,
                expected: false,
            },
            // ANYTHING in statement position matches any statement kind, not only
            // an expression statement (here, an `if`).
            Case {
                selector: "function f() { ANYTHING; return 1; }",
                subject: "function f() { if (x) y(); return 1; }",
                alpha: false,
                expected: true,
            },
            // STR_LITERAL_MATCHING_RE matches a string literal whose value matches
            // the pattern — by regex, not by structure.
            Case {
                selector: "const a = STR_LITERAL_MATCHING_RE(\"^x\");",
                subject: "const a = \"xyz\";",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "const a = STR_LITERAL_MATCHING_RE(\"^x\");",
                subject: "const a = \"abc\";",
                alpha: false,
                expected: false,
            },
            // the predicate only matches a string literal — a non-string subject
            // (here a number) never matches.
            Case {
                selector: "const a = STR_LITERAL_MATCHING_RE(\"foo\");",
                subject: "const a = 5;",
                alpha: false,
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
fn multi_statement_sequence_aligns_around_a_stmt_list_hole() {
    js_ast::with_swc_globals(|| {
        // Two fixed statements with a STMT_LIST hole between them; the hole
        // absorbs the intervening body statements (module-level subsequence).
        let needle = roots("const a = first();\nSTMT_LIST;\nconst b = second();");
        let subject = roots("const a = first();\nx();\ny();\nconst b = second();");
        let alignments = selector_match::match_top_level_sequence(&needle, &subject, Mode::Exact)
            .expect("supported multi-statement needle");
        // `const a` pins body 0, `const b` pins body 3; the hole spans 1..3.
        assert_eq!(alignments, vec![vec![Some(0), None, Some(3)]]);
    });
}

#[test]
fn multi_statement_sequence_enumerates_all_alignments() {
    js_ast::with_swc_globals(|| {
        // `foo();` matches two body positions, so there are two alignments —
        // the matcher must enumerate both (categoricity at the resolver level).
        let needle = roots("STMT_LIST;\nfoo();");
        let subject = roots("foo();\nbar();\nfoo();");
        let alignments = selector_match::match_top_level_sequence(&needle, &subject, Mode::Exact)
            .expect("supported multi-statement needle");
        assert_eq!(alignments, vec![vec![None, Some(0)], vec![None, Some(2)]]);
    });
}

#[test]
fn fail_closed_on_malformed_regex_predicate() {
    js_ast::with_swc_globals(|| {
        // A `STR_LITERAL_MATCHING_RE` that is not a well-formed predicate (here,
        // no argument) is not lowered — the callee keyword is reserved, so the
        // matcher errors rather than treating it as an ordinary call/identifier.
        let result = selector_match::matches(
            &facts("const a = STR_LITERAL_MATCHING_RE();"),
            &facts("const a = b();"),
            Mode::Exact,
        );
        assert!(
            matches!(result, Err(selector_match::Unsupported { .. })),
            "malformed regex predicate must be fail-closed, got {result:?}",
        );
    });
}

#[test]
fn fail_closed_on_misplaced_run_hole() {
    js_ast::with_swc_globals(|| {
        // `ARGS` in expression position (not an argument list) is a misplaced run
        // hole: it reaches the node matcher rather than being consumed as a list
        // carrier, so the match errors rather than treating it as an identifier.
        let result = selector_match::matches(
            &facts("const a = ARGS;"),
            &facts("const a = b;"),
            Mode::Exact,
        );
        assert!(
            matches!(result, Err(selector_match::Unsupported { .. })),
            "misplaced run hole must be fail-closed, got {result:?}",
        );
    });
}
