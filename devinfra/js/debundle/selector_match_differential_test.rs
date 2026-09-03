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
            // ANYTHING absorbs the surrounding properties; a two-hole list
            // with an interior fixed segment (the corpus `{…, k, …}` shape).
            Case {
                selector: "const o = { a: 1, ANYTHING };",
                subject: "const o = { a: 1, b: 2 };",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "const o = { ANYTHING, k: 1, ANYTHING };",
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
            // `extends X { ANYTHING; }` class lists over-match.
            Case {
                selector: "class A extends B { ANYTHING; }",
                subject: "class C extends D { m() {} }",
                alpha: true,
                expected: true,
            },
            Case {
                selector: "class A extends B { ANYTHING; }",
                subject: "class C { m() {} }",
                alpha: true,
                expected: false,
            },
            Case {
                selector: "class A { ANYTHING; }",
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

// A hole-valued key-value property (`{ k: ANYTHING }`, object pattern or object
// literal) must keep its single-node-hole identity: `ANYTHING` matches any one
// value, not alpha-bind as a real identifier. Regression for the parity gap where
// `shorthand_property_view` misclassified `{ k: ANYTHING }` as a same-name
// property and fed `ANYTHING` into the alpha bijection — failing exact mode
// (`ANYTHING` != the name) and conflicting when several `ANYTHING` targets shared
// one frame. These shapes (destructured params, inlined style objects) are
// pervasive in the real tana/re selectors AstWildcardMatcher resolved.
#[test]
fn key_value_hole_value_matches_any() {
    js_ast::with_swc_globals(|| {
        let cases = [
            // hole destructure target: exact (no name to alpha-bind) and alpha.
            (
                "pat hole exact",
                "const { a: ANYTHING } = n;",
                "const { a: t } = n;",
                false,
            ),
            (
                "pat hole alpha",
                "const { a: ANYTHING } = n;",
                "const { a: t } = n;",
                true,
            ),
            // several hole targets in one pattern: independent holes, no bijection
            // conflict (the bug forced one `ANYTHING` binding for all of them).
            (
                "pat two holes",
                "const { a: ANYTHING, b: ANYTHING } = n;",
                "const { a: t, b: r } = n;",
                true,
            ),
            // a real binding beside a hole binding: alpha-bind the real, hole the rest.
            (
                "pat real plus hole",
                "const { a: keep, b: ANYTHING } = n;",
                "const { a: t, b: r } = n;",
                true,
            ),
            // object literal (reference value), not a pattern.
            (
                "literal hole value",
                "const a = foo({ x: ANYTHING });",
                "const a = foo({ x: 1 });",
                true,
            ),
            // a real same-name property still alpha-binds (the fix must not regress it).
            (
                "pat same-name alpha",
                "const { a: y } = n;",
                "const { a: t } = n;",
                true,
            ),
            // full real shape: arrow with destructured-target params + STMT_LIST tail.
            (
                "destructure-param arrow",
                "const X = (ANYTHING, ANYTHING) => { const { nodeContext: ANYTHING, parameters: ANYTHING, stackFrame: ANYTHING } = ANYTHING, { node: ANYTHING } = ANYTHING; if (ANYTHING) throw new Error(\"Stack frame is not defined\"); STMT_LIST; };",
                "const X = (n, e) => { const { nodeContext: t, parameters: r, stackFrame: s } = n, { node: o } = t; if (!s) throw new Error(\"Stack frame is not defined\"); let l = 1; };",
                true,
            ),
        ];
        for (label, selector, subject, alpha) in cases {
            let mode = if alpha { Mode::AlphaAll } else { Mode::Exact };
            let got = selector_match::matches(&facts(selector), &facts(subject), mode)
                .unwrap_or_else(|e| panic!("{label}: Unsupported({})", e.reason));
            assert!(got, "{label}: expected match, got false");
        }
    });
}

// A concise arrow whose body is a parenthesized object literal (`() => ({ … })`)
// is the idiomatic component/factory shape — the returned object is the
// re-minify-stable anchor. The fact extractor sees through the body paren
// (`Expr::Paren` is transparent), so the object fields project as an `Object`
// node and are matchable as anchors. Pins: the construct matches; a `=> ({…})`
// needle must NOT match a block-bodied arrow (`=> { return … }`) — the two are
// different shapes; alpha bindings flow from the params into the object body;
// and an object-property run hole works inside the returned object. Closes the
// SELECTOR_BUGS.md "arrow whose body is a parenthesized object literal" gap.
#[test]
fn arrow_returning_object_literal_matches_on_its_object_anchors() {
    js_ast::with_swc_globals(|| {
        let cases = [
            // exact: the returned object's stable key + literal anchor it.
            Case {
                selector: "const makeWidget = (props) => ({ kind: \"widget\" });",
                subject: "const makeWidget = (props) => ({ kind: \"widget\" });",
                alpha: false,
                expected: true,
            },
            // alpha: the function and param names are renamable; the object's
            // `kind: "widget"` anchor and the param→body identifier flow hold.
            Case {
                selector: "const X = (props) => ({ kind: \"widget\", render: () => props.label });",
                subject: "const Y = (a) => ({ kind: \"widget\", render: () => a.label });",
                alpha: true,
                expected: true,
            },
            // a distinct stable key in the returned object is a real mismatch.
            Case {
                selector: "const X = (props) => ({ kind: \"widget\" });",
                subject: "const Y = (a) => ({ shape: \"widget\" });",
                alpha: true,
                expected: false,
            },
            // the param identifier must be the same binding the object body reads.
            Case {
                selector: "const X = (props) => ({ value: props });",
                subject: "const Y = (a) => ({ value: other });",
                alpha: true,
                expected: false,
            },
            // an object-returning arrow must NOT match a block-bodied arrow that
            // returns the same object — `=> ({…})` and `=> { return {…} }` are
            // distinct shapes (the gap was reading the body `{` as a block).
            Case {
                selector: "const X = (props) => ({ kind: \"widget\" });",
                subject: "const Y = (a) => { return { kind: \"widget\" }; };",
                alpha: true,
                expected: false,
            },
            // The object run hole inside the returned object absorbs the noisy generated
            // members, leaving the one stable key pinned.
            Case {
                selector: "const X = (props) => ({ kind: \"widget\", ANYTHING });",
                subject: "const Y = (a) => ({ kind: \"widget\", render: () => a.label, dispose() {} });",
                alpha: true,
                expected: true,
            },
        ];
        for case in cases {
            let mode = if case.alpha {
                Mode::AlphaAll
            } else {
                Mode::Exact
            };
            let got = selector_match::matches(&facts(case.selector), &facts(case.subject), mode)
                .expect("arrow-returns-object is within the faithful subset");
            assert_eq!(
                got, case.expected,
                "arrow-returns-object: {:?} vs {:?} (alpha={})",
                case.selector, case.subject, case.alpha,
            );
        }
    });
}

// A `return` (or arrow body) of a parenthesized sequence/assignment expression
// is the distinctive body of two recurring helpers: the esbuild/TypeScript
// `__decorate` shape `(applyDecorators(t, d), t)` and the memoized-singleton
// (lazy accessor) idiom `(instance || (instance = build()), instance)`. The
// extractor preserves the `Seq` and the inner `Assign` (parens are transparent
// grouping, the structure is kept), so these bodies match structurally — the
// inner lazy-init assignment is a real anchor, not lost. Pins: the construct
// matches (exact + alpha); a near-miss that drops the lazy-init assignment
// fails; sequence arity is significant. Closes the SELECTOR_BUGS.md
// "parenthesized sequence or assignment expression body" gap.
#[test]
fn parenthesized_sequence_body_matches_on_its_inner_assignment() {
    js_ast::with_swc_globals(|| {
        let cases = [
            // memoized singleton: the `instance || (instance = build())` lazy init
            // followed by the returned `instance` is the whole distinctive body.
            Case {
                selector: "function getSingleton() { return (instance || (instance = build()), instance); }",
                subject: "function getSingleton() { return (instance || (instance = build()), instance); }",
                alpha: false,
                expected: true,
            },
            // alpha: the accessor + the cached binding are renamable; the
            // `|| (x = build())`-then-`x` sequence structure holds.
            Case {
                selector: "function getSingleton() { return (instance || (instance = build()), instance); }",
                subject: "function h() { return (cached || (cached = build()), cached); }",
                alpha: true,
                expected: true,
            },
            // near-miss: the lazy-init assignment is gone (`(instance, instance)`),
            // so the structurally-distinctive part is absent — must not match.
            Case {
                selector: "function getSingleton() { return (instance || (instance = build()), instance); }",
                subject: "function getSingleton() { return (instance, instance); }",
                alpha: false,
                expected: false,
            },
            // esbuild decorate-helper: `(applyDecorators(target, decorators), target)`.
            Case {
                selector: "function decorate(target, decorators) { return (applyDecorators(target, decorators), target); }",
                subject: "function decorate(target, decorators) { return (applyDecorators(target, decorators), target); }",
                alpha: false,
                expected: true,
            },
            // a single-node EXPR hole as the sequence's first element, anchoring on
            // the returned identifier — the run-effect statement is noise.
            Case {
                selector: "function f() { return (EXPR, value); }",
                subject: "function f() { return (sideEffect(), value); }",
                alpha: false,
                expected: true,
            },
            // sequence arity is significant: a two-element sequence is not a
            // three-element one.
            Case {
                selector: "function f() { return (a, b); }",
                subject: "function f() { return (a, b, c); }",
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
            let got = selector_match::matches(&facts(case.selector), &facts(case.subject), mode)
                .expect("parenthesized sequence body is within the faithful subset");
            assert_eq!(
                got, case.expected,
                "parenthesized-sequence-body: {:?} vs {:?} (alpha={})",
                case.selector, case.subject, case.alpha,
            );
        }
    });
}

// `ARRAY_ELEMENTS` is the array-literal run hole (the object-property analog for
// arrays): a bare identifier element absorbing a run of array elements, so a long
// array initializer can be anchored on its few stable elements without spelling
// the rest. It is matched as an ordered subsequence with gaps, exactly like the
// other run holes (the carriers partition the needle into fixed segments). Closes
// the SELECTOR_BUGS.md "no array-element-run hole" gap.
#[test]
fn array_elements_run_hole_anchors_a_few_stable_elements() {
    js_ast::with_swc_globals(|| {
        let cases = [
            // anchor-first then the run absorbs the rest (any length, incl. empty).
            Case {
                selector: "const c = [\"keep\", ARRAY_ELEMENTS];",
                subject: "const c = [\"keep\", 1, 2, 3];",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "const c = [\"keep\", ARRAY_ELEMENTS];",
                subject: "const c = [\"keep\"];",
                alpha: false,
                expected: true,
            },
            // the anchored-left fixed element must still match.
            Case {
                selector: "const c = [\"keep\", ARRAY_ELEMENTS];",
                subject: "const c = [\"other\", 1];",
                alpha: false,
                expected: false,
            },
            // run then an anchored-right element; the last element must match.
            Case {
                selector: "const c = [ARRAY_ELEMENTS, \"last\"];",
                subject: "const c = [1, 2, \"last\"];",
                alpha: false,
                expected: true,
            },
            Case {
                selector: "const c = [ARRAY_ELEMENTS, \"last\"];",
                subject: "const c = [1, 2, \"nope\"];",
                alpha: false,
                expected: false,
            },
            // two run holes bracket one interior anchor.
            Case {
                selector: "const c = [ARRAY_ELEMENTS, \"mid\", ARRAY_ELEMENTS];",
                subject: "const c = [1, \"mid\", 2, 3];",
                alpha: false,
                expected: true,
            },
            // the `[...spread, EXTRA]` shape: the run absorbs the spread, the
            // trailing element is pinned.
            Case {
                selector: "const c = [ARRAY_ELEMENTS, EXTRA];",
                subject: "const c = [...base, EXTRA];",
                alpha: false,
                expected: true,
            },
            // an all-holes array pins nothing — matches any array.
            Case {
                selector: "const c = [ARRAY_ELEMENTS];",
                subject: "const c = [1, 2, 3];",
                alpha: false,
                expected: true,
            },
            // alpha: a fixed-element identifier still alpha-binds across the run.
            Case {
                selector: "const c = [first, ARRAY_ELEMENTS];",
                subject: "const d = [renamed, 9, 8];",
                alpha: true,
                expected: true,
            },
        ];
        for case in cases {
            let mode = if case.alpha {
                Mode::AlphaAll
            } else {
                Mode::Exact
            };
            let got = selector_match::matches(&facts(case.selector), &facts(case.subject), mode)
                .expect("ARRAY_ELEMENTS is within the faithful subset");
            assert_eq!(
                got, case.expected,
                "array-elements-run-hole: {:?} vs {:?} (alpha={})",
                case.selector, case.subject, case.alpha,
            );
        }
    });
}

// `ARRAY_ELEMENTS` outside an array-element list (here, in expression position) is
// a misplaced run-hole keyword: it reaches the node matcher rather than being
// consumed as a list carrier, so the match fails closed with `Unsupported` rather
// than treating the keyword as an ordinary identifier — the same fail-closed
// contract the other run holes hold.
#[test]
fn fail_closed_on_misplaced_array_elements_hole() {
    js_ast::with_swc_globals(|| {
        let result = selector_match::matches(
            &facts("const c = ARRAY_ELEMENTS;"),
            &facts("const c = x;"),
            Mode::Exact,
        );
        assert!(
            matches!(result, Err(selector_match::Unsupported { .. })),
            "misplaced ARRAY_ELEMENTS must be fail-closed, got {result:?}",
        );
    });
}

// Same-arity declarators in one `const a = …, b = …` comma-list that differ only
// in a deeply-nested value are disambiguated by a single-declarator selector that
// asserts that nested anchor. The resolver matches a single-declarator needle
// against each declarator of an owner (it synthesizes a single-declarator subject
// per declarator), so these subjects are exactly what the needle is matched
// against here: a needle pinning the nested `"POST"` matches only the POST
// sibling, not the same-shape `"GET"` one. Closes the SELECTOR_BUGS.md
// "comma-list sibling disambiguation by nested body" gap.
#[test]
fn comma_list_siblings_disambiguated_by_nested_value() {
    js_ast::with_swc_globals(|| {
        // The two single-declarator subjects the resolver synthesizes from one
        // comma-list `const handlerA = …, handlerB = …;` owner.
        let get_sibling = "const handlerA = makeHandler({ route: { method: \"GET\" } });";
        let post_sibling = "const handlerB = makeHandler({ route: { method: \"POST\" } });";
        let cases = [
            // the nested `"POST"` anchor matches only the POST sibling.
            (
                "const X = makeHandler({ route: { method: \"POST\" } });",
                post_sibling,
                true,
            ),
            (
                "const X = makeHandler({ route: { method: \"POST\" } });",
                get_sibling,
                false,
            ),
            // and the `"GET"` anchor only the GET sibling.
            (
                "const X = makeHandler({ route: { method: \"GET\" } });",
                get_sibling,
                true,
            ),
            (
                "const X = makeHandler({ route: { method: \"GET\" } });",
                post_sibling,
                false,
            ),
            // a selector that holes the nested value (`method: EXPR`) is the
            // ambiguous case — it matches BOTH siblings, which is exactly why a
            // nested anchor is needed to pin one of them.
            (
                "const X = makeHandler({ route: { method: EXPR } });",
                get_sibling,
                true,
            ),
            (
                "const X = makeHandler({ route: { method: EXPR } });",
                post_sibling,
                true,
            ),
        ];
        for (selector, subject, expected) in cases {
            let got = selector_match::matches(&facts(selector), &facts(subject), Mode::AlphaAll)
                .expect("nested-value selector is within the faithful subset");
            assert_eq!(
                got, expected,
                "comma-list-sibling: {selector:?} vs {subject:?}"
            );
        }
    });
}
