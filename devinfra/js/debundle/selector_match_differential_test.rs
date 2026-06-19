//! Golden semantics of the fact-based matcher (`selector_match::matches`, over
//! the `chunk_facts` EDB): exact and alpha identifier modes, expression-position
//! single-node holes, run holes, declarator alignment, and per-function alpha
//! scoping — each `(selector, subject)` pair asserts the expected match verdict,
//! plus fail-closed (`Unsupported`) outside the faithful subset. These pin the
//! matcher's behavior directly; the corpus-wide gate covers real specs.

use selector_match::Mode;

fn facts(source: &str) -> chunk_facts::ChunkFacts {
    chunk_facts::extract_facts(&js_ast::parse_js_module_ast("<t>", source).unwrap()).unwrap()
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
            // async / generator are part of a function's identity: an async-fn
            // needle must not match a non-async fn (and vice versa), matching
            // production's `eq_ignore_span` on `is_async`/`is_generator`.
            Case {
                selector: "async function f(a) { STMT_LIST; return a; }",
                subject: "async function g(x) { y(); return x; }",
                alpha: true,
                expected: true,
            },
            Case {
                selector: "async function f(a) { STMT_LIST; return a; }",
                subject: "function g(x) { y(); return x; }",
                alpha: true,
                expected: false,
            },
            Case {
                selector: "function f(a) { STMT_LIST; return a; }",
                subject: "async function g(x) { y(); return x; }",
                alpha: true,
                expected: false,
            },
            Case {
                selector: "function* f() { STMT_LIST; }",
                subject: "function* g() { y(); }",
                alpha: true,
                expected: true,
            },
            Case {
                selector: "function* f() { STMT_LIST; }",
                subject: "function g() { y(); }",
                alpha: true,
                expected: false,
            },
            // A class's superclass (`extends`) is part of its identity: present in
            // both (alpha-matched), or absent in both. A no-`extends` needle must
            // NOT match an `extends`-bearing class, and vice versa (mirrors
            // production's `match_class` `super_class` arm) — the gap that made the
            // `extends X { CLASS_REST; }` class lists over-match.
            Case {
                selector: "class A extends B { CLASS_REST; }",
                subject: "class C extends D { m() {} }",
                alpha: true,
                expected: true,
            },
            Case {
                selector: "class A extends B { CLASS_REST; }",
                subject: "class C { m() {} }",
                alpha: true,
                expected: false,
            },
            Case {
                selector: "class A { CLASS_REST; }",
                subject: "class C extends D { m() {} }",
                alpha: true,
                expected: false,
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
            let mode = if case.alpha {
                Mode::AlphaAll
            } else {
                Mode::Exact
            };
            let fact = selector_match::matches(&facts(case.selector), &facts(case.subject), mode)
                .expect("case is within the faithful subset");
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
fn var_declarator_alignment_pins_target_through_holes() {
    js_ast::with_swc_globals(|| {
        // A pinned `c` declarator between two `DECLARATORS` holes: the holes absorb
        // declarators 0 and 2, the pinned segment aligns greedy-leftmost to the
        // string-literal declarator at index 1 (alpha: `c` binds to `q`).
        let needle =
            facts("const DECLARATORS_BEFORE = null, c = \"abc\", DECLARATORS_AFTER = null;");
        let subject = facts("const p = 1, q = \"abc\", r = 2;");
        let alignment =
            selector_match::var_declarator_alignment(&needle, &subject, Mode::AlphaAll, None)
                .expect("supported needle")
                .expect("the pinned declarator matches");
        assert_eq!(alignment, vec![None, Some(1), None]);
    });
}

#[test]
fn var_declarator_alignment_prebinding_forces_target_identity() {
    js_ast::with_swc_globals(|| {
        // Two subject declarators carry the same init; prebinding the needle's `c`
        // to the second subject binding pins the alignment to declarator 1 (the
        // production declarator-hole resolver's per-candidate prebinding).
        let needle = facts("const c = \"abc\", DECLARATORS_AFTER = null;");
        let subject = facts("const x = \"abc\", y = \"abc\";");
        let to_first = selector_match::var_declarator_alignment(
            &needle,
            &subject,
            Mode::AlphaAll,
            Some(("c", "x")),
        )
        .expect("supported")
        .expect("matches x");
        assert_eq!(to_first, vec![Some(0), None]);
        let to_second = selector_match::var_declarator_alignment(
            &needle,
            &subject,
            Mode::AlphaAll,
            Some(("c", "y")),
        )
        .expect("supported");
        // `c` is anchored-left (no leading hole), so prebinding it to `y`
        // (declarator 1) cannot align to the anchored position 0 → no match.
        assert_eq!(to_second, None);
    });
}

#[test]
fn var_declarator_alignment_rejects_kind_mismatch() {
    js_ast::with_swc_globals(|| {
        // `let` needle against a `const` subject: the declarators would align, but
        // the `var`/`let`/`const` kind differs, so the statements do not match.
        let needle = facts("let DECLARATORS_BEFORE = null, c = \"abc\";");
        let subject = facts("const q = \"abc\";");
        assert_eq!(
            selector_match::var_declarator_alignment(&needle, &subject, Mode::AlphaAll, None)
                .expect("supported needle"),
            None,
        );
    });
}

#[test]
fn alpha_scopes_per_function_so_reused_param_names_stay_independent() {
    js_ast::with_swc_globals(|| {
        // Two functions reuse the param spelling `p` in distinct scopes; the
        // subject uses *different* param names (e, t). Scope-aware alpha matches —
        // each function's param is an independent binding — where a flat bijection
        // would force `p`↔`e` then reject `p`↔`t` and find no alignment.
        let needle = roots("function a(p){ g = p; }\nfunction b(p){ h = p; }");
        let subject = roots("function x(e){ M = e; }\nfunction y(t){ N = t; }");
        let alignments =
            selector_match::match_top_level_sequence(&needle, &subject, Mode::AlphaAll)
                .expect("supported");
        assert_eq!(alignments, vec![vec![Some(0), Some(1)]]);
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
