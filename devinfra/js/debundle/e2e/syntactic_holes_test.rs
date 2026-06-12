//! End-to-end coverage for `source_match` syntactic holes.
//!
//! Each hole keyword has a bare anonymous form (matches independently,
//! never binds) and a named `KEYWORD_name` form (binds for
//! cross-occurrence equality).
//!
//! Single-node holes:
//! - Expression holes (`EXPR` / `EXPR_name`) are selector-local
//!   identifier expressions; they match one arbitrary expression subtree.
//! - Statement holes (`STMT` / `STMT_name`) are selector-local bare
//!   expression statements; they match exactly one statement.
//!
//! List holes (variable-length):
//! - `STMT_LIST` / `STMT_LIST_name;` in a block body absorbs a run of
//!   statements (including an empty run) — e.g. a method body you don't
//!   want to pin.
//! - `CLASS_REST;` as a class field absorbs a run of class members —
//!   e.g. "match this class by these members, ignore the rest".
//!
//! Several list holes may appear in one block or class body: they split
//! the pinned statements/members into an ordered subsequence with gaps,
//! so a selector can bracket a few stable members with `CLASS_REST;`
//! holes and match any class that contains them in that order.

use debundle_e2e_support::*;

#[test]
fn member_source_match_expr_prefix_holes_match_arbitrary_expression_subtrees() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const actual = Math.max(Number.parseInt("7", 10), [1, 2, 3].length);
console.log(actual);
export { actual };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha(
                "calc_value",
                r#"const readable = Math.max(EXPR_LEFT, EXPR_RIGHT);"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "7\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &["calc_value"],
        &["actual"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &[
            "Math.max",
            r#"Number.parseInt("7", 10)"#,
            "].length",
            "const calc_value",
        ],
        &[],
    );
}

#[test]
fn member_source_match_many_expr_holes_match_positionally() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const actual = [
  1 + 2,
  Number.parseInt("4", 10),
  ({ x: 5 }).x,
  [6, 7].length,
  Math.max(8, 9),
  true ? 10 : 11,
];
console.log(actual.length);
export { actual };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha(
                "calc_value",
                r#"const readable = [
  EXPR_A,
  EXPR_B,
  EXPR_C,
  EXPR_D,
  EXPR_E,
  EXPR_F,
];"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "6\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &[
            "1 + 2",
            r#"Number.parseInt("4", 10)"#,
            "x: 5",
            "}).x",
            "7",
            "].length",
            "Math.max(8, 9)",
            "true ? 10 : 11",
        ],
        &[],
    );
}

#[test]
fn binding_group_source_match_expr_prefix_holes_match_each_target_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var first = 1 + 2, second = Number.parseInt("4", 10);
console.log(first + second);
export { first, second };
"#,
        vec![logical_module_with_binding_groups(
            "pair",
            &[],
            &[BindingGroup::source_alpha(
                r#"var left = EXPR_LEFT, right = EXPR_RIGHT;"#,
                &[("left", "first_value"), ("right", "second_value")],
            )],
        )],
    ));

    assert_entry_output(&fixture, "7\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/pair.js",
        &["first_value", "second_value"],
        &["first", "second"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/pair.js",
        &[
            "var first_value = 1 + 2",
            r#"var second_value = Number.parseInt("4", 10)"#,
        ],
        &[],
    );
}

#[test]
fn binding_group_comments_emit_for_each_target_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var first = 1 + 2, second = Number.parseInt("4", 10);
console.log(first + second);
export { first, second };
"#,
        vec![logical_module_with_binding_groups(
            "pair",
            &[],
            &[BindingGroup::source_alpha(
                r#"var left = EXPR_LEFT, right = EXPR_RIGHT;"#,
                &[("left", "first_value"), ("right", "second_value")],
            )
            .with_comments(&[
                ("left", "First selected value."),
                ("right", "Second selected value."),
            ])],
        )],
    ));

    assert_entry_output(&fixture, "7\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/pair.js",
        &[
            "// First selected value.",
            "var first_value = 1 + 2",
            "// Second selected value.",
            r#"var second_value = Number.parseInt("4", 10)"#,
        ],
        &[],
    );
}

#[test]
fn anonymous_source_match_stmt_prefix_hole_matches_arbitrary_nested_statement() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("setup");
  console.log("done");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT_SETUP;
  console.log("done");
}"#,
        )],
    ));

    assert_entry_output(&fixture, "setup\ndone\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/init.js",
        &[
            r#"console.log("setup")"#,
            r#"console.log("done")"#,
            "const marker",
        ],
        &[],
    );
}

#[test]
fn anonymous_source_match_stmt_prefix_holes_still_reject_ambiguous_matches() {
    let opts = FixtureOpts::new(
        r#"if (true) {
  console.log("first");
  console.log("done");
}
const marker = "ready";
if (true) {
  console.log("second");
  console.log("done");
}
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT_SETUP;
  console.log("done");
}"#,
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::init",
            "ambiguous",
            "STMT_SETUP",
            r#"console.log("done")"#,
        ],
    );
}

#[test]
fn anonymous_source_match_stmt_list_hole_absorbs_contiguous_statements() {
    // `STMT_LIST_BODY;` as the whole block body absorbs the three
    // statements, so the selector matches the `if` regardless of body.
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("a");
  console.log("b");
  console.log("c");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT_LIST_BODY;
}"#,
        )],
    ));

    assert_entry_output(&fixture, "a\nb\nc\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/init.js",
        &[
            r#"console.log("a")"#,
            r#"console.log("b")"#,
            r#"console.log("c")"#,
            "const marker",
        ],
        // The selector's placeholder name never appears in the output;
        // the original statements were spliced in verbatim.
        &["STMT_LIST_BODY"],
    );
}

#[test]
fn anonymous_source_match_stmt_list_hole_absorbs_empty_run() {
    // A trailing `STMT_LIST_TAIL;` matches a block that has only the
    // pinned prefix statement — the hole absorbs zero statements.
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("only");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  console.log("only");
  STMT_LIST_TAIL;
}"#,
        )],
    ));

    assert_entry_output(&fixture, "only\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/init.js",
        &[r#"console.log("only")"#, "const marker"],
        &["STMT_LIST_TAIL"],
    );
}

#[test]
fn member_source_match_class_rest_hole_selects_class_ignoring_other_members() {
    // Pin the class by its constructor (body hole) and let `CLASS_REST;`
    // absorb `increment` and `reset`. The whole class still moves — the
    // hole is only in the selector, not the output.
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Counter {
  constructor() {
    this.value = 0;
  }
  increment() {
    this.value += 1;
    return this.value;
  }
  reset() {
    this.value = 0;
  }
}
const counter = new Counter();
console.log(counter.increment());
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Counter",
                r#"class K {
  constructor() {
    STMT_LIST_CTOR;
  }
  CLASS_REST;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Counter"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        // The full class moved, members and all.
        &["class", "increment", "reset"],
        &["CLASS_REST", "STMT_LIST_CTOR"],
    );
}

#[test]
fn member_source_match_class_skeleton_rejects_ambiguous_match() {
    // The skeleton `class K { run() { STMT_LIST } CLASS_REST }` matches
    // both `Alpha` and `Beta`; ambiguous matches stay hard errors.
    let opts = FixtureOpts::new(
        r#"class Alpha {
  run() {
    return 1;
  }
}
class Beta {
  run() {
    return 2;
  }
}
console.log(new Alpha().run() + new Beta().run());
export { Alpha };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Selected",
                r#"class K {
  run() {
    STMT_LIST_BODY;
  }
  CLASS_REST;
}"#,
            )],
        )],
    );

    expect_rejection_containing_all(opts, &["static/app::shapes", "ambiguous"]);
}

#[test]
fn member_source_match_class_rest_hole_pins_member_order() {
    // CLASS_REST is positional: members pinned before the hole must be
    // the candidate's leading members in the same order. Listing `b`
    // before `a` does not match a class whose first members are `a`
    // then `b`, so resolution finds no match.
    let opts = FixtureOpts::new(
        r#"class Counter {
  a() {
    return 1;
  }
  b() {
    return 2;
  }
}
console.log(new Counter().a());
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Selected",
                r#"class K {
  b() {
    STMT_LIST_B;
  }
  a() {
    STMT_LIST_A;
  }
  CLASS_REST;
}"#,
            )],
        )],
    );

    expect_rejection_containing_all(opts, &["static/app::shapes", "did not match"]);
}

#[test]
fn anonymous_expr_holes_match_independent_subtrees() {
    // The bare keyword `EXPR` is anonymous: the two occurrences match
    // *different* expressions. A named hole `EXPR_X` repeated would
    // instead force the two arguments to be equal.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const actual = Math.max(Number.parseInt("7", 10), [1, 2, 3].length);
console.log(actual);
export { actual };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha(
                "calc_value",
                r#"const readable = Math.max(EXPR, EXPR);"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "7\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &["Math.max", "const calc_value"],
        &["EXPR"],
    );
}

#[test]
fn anonymous_stmt_hole_matches_one_arbitrary_statement() {
    // The bare keyword `STMT` matches exactly one statement, with no
    // suffix to mint — the anonymous single-statement form.
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("setup");
  console.log("done");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT;
  console.log("done");
}"#,
        )],
    ));

    assert_entry_output(&fixture, "setup\ndone\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/init.js",
        &[
            r#"console.log("setup")"#,
            r#"console.log("done")"#,
            "const marker",
        ],
        &["STMT"],
    );
}

#[test]
fn anonymous_stmt_list_and_class_rest_holes_need_no_minted_names() {
    // Bare `STMT_LIST` and bare `CLASS_REST` select the class with no
    // suffixes to invent.
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Counter {
  constructor() {
    this.value = 0;
  }
  increment() {
    this.value += 1;
    return this.value;
  }
}
const counter = new Counter();
console.log(counter.increment());
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Counter",
                r#"class K {
  constructor() {
    STMT_LIST;
  }
  CLASS_REST;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Counter"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["class", "increment"],
        &["STMT_LIST", "CLASS_REST"],
    );
}

#[test]
fn member_source_match_class_rest_holes_bracket_interior_member() {
    // Two `CLASS_REST;` holes bracket a single pinned member, so the
    // selector matches a class by an interior member it contains: the
    // leading hole absorbs `a`, the trailing hole absorbs `c`, and `b`
    // is pinned in between. (Previously a second `CLASS_REST` was a hard
    // "ambiguous, never matches"; it is now an ordered-subsequence gap.)
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Counter {
  a() {
    return 1;
  }
  b() {
    return 2;
  }
  c() {
    return 3;
  }
}
console.log(new Counter().b());
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Counter",
                r#"class K {
  CLASS_REST;
  b() {
    STMT_LIST_B;
  }
  CLASS_REST;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "2\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Counter"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        // The whole class moved; the bracketing holes are selector-only.
        &["class", "a()", "b()", "c()"],
        &["CLASS_REST", "STMT_LIST_B"],
    );
}

#[test]
fn member_source_match_interleaved_class_rest_holes_match_ordered_members() {
    // Two pinned members separated by a `CLASS_REST;` hole match a class
    // that contains them in that order with other members interspersed:
    // `open` (after `setup`) then `close` (after `tick`). This is the
    // ordered-subset fingerprint — pin a few stable members, ignore the
    // rest.
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Widget {
  setup() {
    return 0;
  }
  open() {
    return 1;
  }
  tick() {
    return 2;
  }
  close() {
    return 3;
  }
}
console.log(new Widget().open() + new Widget().close());
export { Widget };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Widget",
                r#"class K {
  CLASS_REST;
  open() {
    STMT_LIST_O;
  }
  CLASS_REST;
  close() {
    STMT_LIST_C;
  }
  CLASS_REST;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "4\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Widget"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["setup()", "open()", "tick()", "close()"],
        &["CLASS_REST"],
    );
}

#[test]
fn member_source_match_interleaved_class_rest_holes_enforce_order() {
    // The same `Widget`, but the selector pins `close` before `open`.
    // Ordered-subsequence matching keeps source order, so pinning them
    // in the wrong order matches nothing — it is not an unordered
    // "contains both somewhere" match.
    let opts = FixtureOpts::new(
        r#"class Widget {
  setup() {
    return 0;
  }
  open() {
    return 1;
  }
  tick() {
    return 2;
  }
  close() {
    return 3;
  }
}
console.log(new Widget().open());
export { Widget };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Selected",
                r#"class K {
  CLASS_REST;
  close() {
    STMT_LIST_C;
  }
  CLASS_REST;
  open() {
    STMT_LIST_O;
  }
  CLASS_REST;
}"#,
            )],
        )],
    );

    expect_rejection_containing_all(opts, &["static/app::shapes", "did not match"]);
}

#[test]
fn anonymous_source_match_multiple_stmt_list_holes_bracket_pinned_statements() {
    // Three `STMT_LIST_*;` holes bracket two pinned statements inside a
    // block: the holes absorb the `a`/`b`/`c` logs, leaving `pinned1`
    // then `pinned2` matched in order.
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("a");
  console.log("pinned1");
  console.log("b");
  console.log("pinned2");
  console.log("c");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT_LIST_HEAD;
  console.log("pinned1");
  STMT_LIST_MID;
  console.log("pinned2");
  STMT_LIST_TAIL;
}"#,
        )],
    ));

    assert_entry_output(&fixture, "a\npinned1\nb\npinned2\nc\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/init.js",
        &[
            r#"console.log("a")"#,
            r#"console.log("pinned1")"#,
            r#"console.log("pinned2")"#,
            "const marker",
        ],
        &["STMT_LIST_HEAD", "STMT_LIST_MID", "STMT_LIST_TAIL"],
    );
}

#[test]
fn non_trailing_class_rest_hole_keeps_later_identifiers_aligned() {
    // Regression guard for the alpha-identifier bijection: a leading
    // `CLASS_REST` absorbs `helper`, whose param/body identifiers do not
    // desync the `run(value) { return value * 2 }` member that follows.
    // (Under the old global alpha-canonicalization the absorbed `helper`
    // identifiers shifted the numbering and this failed to match.)
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Counter {
  helper(seed) {
    return seed + 1;
  }
  run(value) {
    return value * 2;
  }
}
const counter = new Counter();
console.log(counter.run(5));
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Counter",
                r#"class K {
  CLASS_REST;
  run(value) {
    return value * 2;
  }
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "10\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Counter"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["class", "helper", "run"],
        &["CLASS_REST"],
    );
}

#[test]
fn single_node_hole_keeps_later_identifiers_aligned() {
    // The same bijection guard for single-node holes: `EXPR` absorbs a
    // multi-identifier subtree, and the `limit` argument after it still
    // matches by alpha-correspondence rather than by absolute position.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const limit = 4;
const alpha = 1, beta = 2, gamma = 3;
const total = Math.max(Math.min(alpha, beta, gamma), limit);
console.log(total);
export { total };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha(
                "calc_total",
                r#"const readable = Math.max(EXPR, limit);"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "4\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &["Math.max", "const calc_total"],
        &["EXPR"],
    );
}
