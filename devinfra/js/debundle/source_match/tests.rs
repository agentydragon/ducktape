use super::*;

fn selector(match_source: &str) -> AnonymousStatementSelector {
    AnonymousStatementSelector {
        match_source: match_source.to_string(),
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding: None,
        target_statement: None,
        target_statements: None,
        wildcard_string_literals: BTreeSet::new(),
    }
}

fn parse_one(source: &str) -> ModuleItem {
    js_ast::with_swc_globals(|| {
        let mut module = js_ast::parse_js_module_ast("<test>", source).unwrap();
        assert_eq!(module.body.len(), 1);
        module.body.remove(0)
    })
}

fn prefilter_for(
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
) -> VarDeclaratorPrefilter {
    let prepared = PreparedNeedle::new(needle, selector);
    VarDeclaratorPrefilter::new(needle, &prepared)
}

fn single_declarator(item: &ModuleItem) -> &VarDeclarator {
    let var = item_var_decl(item).unwrap();
    assert_eq!(var.decls.len(), 1);
    &var.decls[0]
}

fn single_declarator_init(item: &ModuleItem) -> &Expr {
    single_declarator(item).init.as_deref().unwrap()
}

/// A selector + subject pair driven through `PreparedNeedle::matches`.
struct MatchCase {
    /// What the row exercises (mirrors the intent comment of the original
    /// per-case assertion).
    intent: &'static str,
    selector_src: &'static str,
    subject_src: &'static str,
    expected: bool,
}

fn run_match_cases(cases: &[MatchCase]) {
    js_ast::with_swc_globals(|| {
        for case in cases {
            let sel = selector(case.selector_src);
            let needle = parse_one(&sel.match_source);
            let prepared = PreparedNeedle::new(&needle, &sel);
            let subject = parse_one(case.subject_src);
            assert_eq!(
                prepared.matches(&subject),
                case.expected,
                "{}: selector {:?} vs subject {:?}",
                case.intent,
                case.selector_src,
                case.subject_src,
            );
        }
    });
}

// Selector reused by the `case CASE_REST:` absorption rows below.
const CASE_REST_ABSORB_SELECTOR: &str = r#"function readable(ANYTHING) {
  switch (ANYTHING) {
    case CASE_REST:
    case "go":
      STMT_LIST
    case CASE_REST:
  }
}"#;

// Selector reused by the no-CASE_REST exact-switch rows below.
const SWITCH_NO_REST_SELECTOR: &str = r#"function readable(ANYTHING) {
  switch (ANYTHING) {
    case "a": STMT_LIST
    case "b": STMT_LIST
  }
}"#;

#[test]
fn prepared_needle_matches_via_table() {
    run_match_cases(&[
        // `STR_LITERAL_MATCHING_RE` matches a runtime string literal that
        // satisfies the regex, and rejects one that does not or a non-string.
        MatchCase {
            intent: "regex predicate matches conforming string literal",
            selector_src: r#"const readable = STR_LITERAL_MATCHING_RE("^Card-[0-9]+$");"#,
            subject_src: r#"const minified = "Card-42";"#,
            expected: true,
        },
        MatchCase {
            intent: "regex predicate rejects non-conforming string literal",
            selector_src: r#"const readable = STR_LITERAL_MATCHING_RE("^Card-[0-9]+$");"#,
            subject_src: r#"const minified = "Panel-42";"#,
            expected: false,
        },
        MatchCase {
            intent: "regex predicate rejects non-string initializer",
            selector_src: r#"const readable = STR_LITERAL_MATCHING_RE("^Card-[0-9]+$");"#,
            subject_src: r#"const minified = makeCardName();"#,
            expected: false,
        },
        // `case "go":` with arbitrary cases before and after.
        MatchCase {
            intent: "CASE_REST absorbs surrounding case runs",
            selector_src: CASE_REST_ABSORB_SELECTOR,
            subject_src: r#"function m(t) {
  switch (t) {
    case "a": return 1;
    case "go": doThing(); break;
    case "b": return 2;
    default: return 3;
  }
}"#,
            expected: true,
        },
        // `case "go":` as the very first arm (leading hole absorbs zero).
        MatchCase {
            intent: "leading CASE_REST absorbs zero cases",
            selector_src: CASE_REST_ABSORB_SELECTOR,
            subject_src: r#"function m(t) {
  switch (t) {
    case "go": go();
    case "z": zz();
  }
}"#,
            expected: true,
        },
        // No `case "go":` arm at all — must not match.
        MatchCase {
            intent: "CASE_REST requires the pinned case to be present",
            selector_src: CASE_REST_ABSORB_SELECTOR,
            subject_src: r#"function m(t) {
  switch (t) {
    case "a": return 1;
    case "b": return 2;
  }
}"#,
            expected: false,
        },
        // A switch selector with no CASE_REST hole still matches exactly.
        MatchCase {
            intent: "switch without CASE_REST matches exactly",
            selector_src: SWITCH_NO_REST_SELECTOR,
            subject_src: r#"function m(t) {
  switch (t) {
    case "a": one();
    case "b": two();
  }
}"#,
            expected: true,
        },
        // Extra trailing case is rejected without a CASE_REST hole.
        MatchCase {
            intent: "switch without CASE_REST rejects an extra trailing case",
            selector_src: SWITCH_NO_REST_SELECTOR,
            subject_src: r#"function m(t) {
  switch (t) {
    case "a": one();
    case "b": two();
    case "c": three();
  }
}"#,
            expected: false,
        },
    ]);
}

#[test]
fn prepared_needle_matches_borrowed_single_var_declarator() {
    let plain_selector = selector(r#"const readable = makeValue("target-token");"#);
    let needle = parse_one(&plain_selector.match_source);
    let prepared = PreparedNeedle::new(&needle, &plain_selector);
    let candidate = parse_one(
        r#"const before = otherValue("other-token"),
  minified = makeValue("target-token"),
  after = otherValue("other-token");"#,
    );
    let candidate_var = item_var_decl(&candidate).unwrap();

    assert!(!prepared.matches_single_var_declarator(&candidate, &candidate_var.decls[0]));
    assert!(prepared.matches_single_var_declarator(&candidate, &candidate_var.decls[1]));
    assert!(!prepared.matches_single_var_declarator(&candidate, &candidate_var.decls[2]));

    let export_selector = selector(r#"export const readable = makeValue("target-token");"#);
    let export_needle = parse_one(&export_selector.match_source);
    let export_prepared = PreparedNeedle::new(&export_needle, &export_selector);
    let export_candidate = parse_one(
        r#"export const before = otherValue("other-token"),
  minified = makeValue("target-token"),
  after = otherValue("other-token");"#,
    );
    let export_candidate_var = item_var_decl(&export_candidate).unwrap();

    assert!(
        !export_prepared
            .matches_single_var_declarator(&export_candidate, &export_candidate_var.decls[0])
    );
    assert!(
        export_prepared
            .matches_single_var_declarator(&export_candidate, &export_candidate_var.decls[1])
    );
    assert!(
        !export_prepared
            .matches_single_var_declarator(&export_candidate, &export_candidate_var.decls[2])
    );
}

#[test]
fn var_declarator_prefilter_uses_string_literal_in_alpha_mode() {
    let selector = selector(r#"const readableName = "stable-css-class";"#);
    let needle = parse_one(&selector.match_source);
    let prefilter = prefilter_for(&needle, &selector);
    let matching = parse_one(r#"const minifiedName = "stable-css-class";"#);
    let non_matching = parse_one(r#"const otherName = "other-css-class";"#);

    assert!(prefilter.declarator_can_match(single_declarator(&matching)));
    assert!(!prefilter.declarator_can_match(single_declarator(&non_matching)));
}

#[test]
fn var_declarator_prefilter_respects_string_literal_wildcards() {
    let mut selector = selector(r#"const readableName = "STRING_HOLE";"#);
    selector
        .wildcard_string_literals
        .insert("STRING_HOLE".to_string());
    let needle = parse_one(&selector.match_source);
    let prefilter = prefilter_for(&needle, &selector);
    let candidate = parse_one(r#"const minifiedName = "runtime-css-class";"#);

    assert!(prefilter.declarator_can_match(single_declarator(&candidate)));
}

#[test]
fn string_literal_regex_predicate_recognizes_only_direct_string_pattern_calls() {
    let selector = parse_one(r#"const readable = STR_LITERAL_MATCHING_RE("^Card-[0-9]+$");"#);
    assert_eq!(
        string_literal_regex_pattern(single_declarator_init(&selector)).as_deref(),
        Some("^Card-[0-9]+$"),
    );

    let non_string_arg = parse_one(r#"const readable = STR_LITERAL_MATCHING_RE(pattern);"#);
    assert!(string_literal_regex_pattern(single_declarator_init(&non_string_arg)).is_none());

    let two_args = parse_one(r#"const readable = STR_LITERAL_MATCHING_RE("Card", "Panel");"#);
    assert!(string_literal_regex_pattern(single_declarator_init(&two_args)).is_none());
}

#[test]
fn string_literal_regex_predicate_selector_has_wildcards() {
    // The regex predicate is a wildcard, so the prepared needle is not a
    // no-wildcards exact match (the table rows above cover the match results).
    let selector = selector(r#"const readable = STR_LITERAL_MATCHING_RE("^Card-[0-9]+$");"#);
    let needle = parse_one(&selector.match_source);
    let prepared = PreparedNeedle::new(&needle, &selector);
    assert!(!prepared.no_wildcards);
}

#[test]
fn declarator_hole_prefilter_uses_regex_string_literal_predicates() {
    let selector = selector(
        r#"const DECLARATORS_BEFORE = null,
  readable = STR_LITERAL_MATCHING_RE("^generic-token-[0-9]+$"),
  DECLARATORS_AFTER = null;"#,
    );
    let needle = parse_one(&selector.match_source);
    let prepared = PreparedNeedle::new(&needle, &selector);
    let prefilter =
        VarDeclWithDeclaratorHolesPrefilter::new(item_var_decl(&needle).unwrap(), &prepared);

    let matching = parse_one(
        r#"const before = "other",
  minified = "generic-token-42",
  after = "other";"#,
    );
    let non_matching = parse_one(
        r#"const before = "other",
  minified = "different-token-42",
  after = "other";"#,
    );

    assert!(prefilter.var_decl_can_match(item_var_decl(&matching).unwrap()));
    assert!(!prefilter.var_decl_can_match(item_var_decl(&non_matching).unwrap()));
}

#[test]
fn declarator_hole_body_index_matching_prefilters_by_literal_predicate() {
    js_ast::with_swc_globals(|| {
        let runtime = js_ast::parse_js_module_ast(
            "<test>",
            r#"const unrelatedA = "generic-token-1";
const before = "other",
  minified = "generic-token-42",
  after = "other";
const unrelatedB = "different-token-42";"#,
        )
        .unwrap();
        let selector = selector(
            r#"const DECLARATORS_BEFORE = null,
  readable = STR_LITERAL_MATCHING_RE("^generic-token-42$"),
  DECLARATORS_AFTER = null;"#,
        );
        let needle = parse_one(&selector.match_source);

        assert_eq!(
            find_matching_body_indices(&runtime, &needle, &selector, BodyIndexFilter::All),
            vec![1]
        );
    });
}

#[test]
fn case_rest_hole_is_recognized_only_as_bare_keyword_empty_case() {
    js_ast::with_swc_globals(|| {
        let item = parse_one("switch (x) { case CASE_REST: }");
        let Stmt::Switch(switch) = &item.as_stmt().unwrap() else {
            panic!("expected switch");
        };
        assert!(is_case_rest_hole(&switch.cases[0]));

        // A `case CASE_REST:` with a body is a real case, not a hole.
        let with_body = parse_one("switch (x) { case CASE_REST: break; }");
        let Stmt::Switch(switch) = &with_body.as_stmt().unwrap() else {
            panic!("expected switch");
        };
        assert!(!is_case_rest_hole(&switch.cases[0]));

        // `default:` is never a hole; nor is an unrelated literal.
        let other = parse_one(r#"switch (x) { default: case "a": }"#);
        let Stmt::Switch(switch) = &other.as_stmt().unwrap() else {
            panic!("expected switch");
        };
        assert!(!is_case_rest_hole(&switch.cases[0]));
        assert!(!is_case_rest_hole(&switch.cases[1]));
    });
}
