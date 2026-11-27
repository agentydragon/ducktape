use super::*;

fn member(match_source: &str, target_binding: Option<&str>) -> AnonymousStatementSelector {
    AnonymousStatementSelector {
        match_source: match_source.to_string(),
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding: target_binding.map(str::to_string),
    }
}

fn module(src: &str) -> Module {
    js_ast::parse_js_module_ast("<test>", src).unwrap()
}

fn group(match_source: &str) -> AnonymousStatementSelector {
    AnonymousStatementSelector {
        match_source: match_source.to_string(),
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding: None,
    }
}

fn exports(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(target, export)| (target.to_string(), export.to_string()))
        .collect()
}

fn parse(selector: &AnonymousStatementSelector) -> ParsedSourceMatchSelector {
    ParsedSourceMatchSelector::parse(
        "test",
        "<test>".to_string(),
        selector,
        SelectorField::SourceMatch,
    )
    .expect("selector parses")
}

/// The claimed binding of every member candidate, body-index ascending.
fn member_names(chunk: &Module, selector: &AnonymousStatementSelector) -> Vec<String> {
    ChunkResolver::new(chunk)
        .member_candidates("test", &parse(selector))
        .expect("member candidates")
        .into_iter()
        .map(|matched| matched.binding)
        .collect()
}

fn group_candidates(
    chunk: &Module,
    selector: &AnonymousStatementSelector,
    exports: &BTreeMap<String, String>,
) -> Vec<MemberBindingGroupMatch> {
    ChunkResolver::new(chunk)
        .member_group_candidates("test", &parse(selector), exports)
        .expect("group candidates")
}

fn anonymous_groups(
    chunk: &Module,
    selector: &AnonymousStatementSelector,
) -> Vec<AnonymousGroupMatch> {
    ChunkResolver::new(chunk)
        .anonymous_group_candidates("test", &parse(selector))
        .expect("anonymous candidates")
}

#[test]
fn chunk_resolver_declarator_hole_group_candidate() {
    js_ast::with_swc_globals(|| {
        // The `*-module_*` binding-group shape: holes around several pinned,
        // string-predicate declarators; one alignment supplies every target.
        let chunk = module("const p = 1, aClass = \"abc\", bClass = \"xyz\", q = 2;\n");
        let selector = group(
            "const DECLARATORS_BEFORE = null, a = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
             DECLARATORS_GAP = null, b = STR_LITERAL_MATCHING_RE(\"^xyz$\"), \
             DECLARATORS_AFTER = null;",
        );
        let candidates = group_candidates(
            &chunk,
            &selector,
            &exports(&[("a", "ExportA"), ("b", "ExportB")]),
        );
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].bindings["a"].binding, "aClass");
        assert_eq!(candidates[0].bindings["b"].binding, "bClass");
    });
}

#[test]
fn chunk_resolver_group_referencing_a_renamed_target_by_chunk_name() {
    js_ast::with_swc_globals(|| {
        // `wrap` references the chunk's `q` by its chunk spelling while the
        // group renames that same declarator to `readable`.
        let chunk = module("const z = 0, w = () => use(q), q = () => 1;\n");
        let selector =
            group("const DECLARATORS_BEFORE = null, wrap = () => use(q), readable = () => 1;");
        let candidates = group_candidates(
            &chunk,
            &selector,
            &exports(&[("wrap", "Wrap"), ("readable", "Readable")]),
        );
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].bindings["wrap"].binding, "w");
        assert_eq!(candidates[0].bindings["readable"].binding, "q");
        assert_eq!(
            candidates[0].free_bindings,
            BTreeMap::from([
                ("q".to_string(), "q".to_string()),
                ("use".to_string(), "use".to_string()),
            ])
        );
    });
}

#[test]
fn chunk_resolver_general_group_candidate() {
    js_ast::with_swc_globals(|| {
        // A multi-statement (general-path) group: a leading anonymous statement
        // then two single-declarator targets, matched as a contiguous window.
        let chunk = module("init();\nconst alpha = makeA();\nconst beta = makeB();\n");
        let selector = group("init();\nconst a = makeA();\nconst b = makeB();");
        let candidates = group_candidates(
            &chunk,
            &selector,
            &exports(&[("a", "ExportA"), ("b", "ExportB")]),
        );
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].bindings["a"].binding, "alpha");
        assert_eq!(candidates[0].bindings["b"].binding, "beta");
    });
}

#[test]
fn chunk_resolver_declarator_inside_multi_declarator_source_unit() {
    js_ast::with_swc_globals(|| {
        // The target init lives in the second declarator of a multi-declarator
        // statement — only declarator-level matching finds it.
        let chunk = module("const a = 1, target = compute();\nconst other = 2;\n");
        let selector = member("const x = compute();", Some("x"));
        assert_eq!(member_names(&chunk, &selector), ["target"]);
    });
}

#[test]
fn chunk_resolver_var_declarator_candidates_include_multi_declarator_source_units() {
    js_ast::with_swc_globals(|| {
        // The same init appears in two declarators (one inside a
        // multi-declarator source_unit): both are candidates. A whole-statement match
        // would miss the multi-declarator one and report a single candidate.
        let chunk = module("const a = 1, b = compute();\nconst c = compute();\n");
        let selector = member("const x = compute();", Some("x"));
        assert_eq!(member_names(&chunk, &selector), ["b", "c"]);
    });
}

#[test]
fn chunk_resolver_declarator_hole_member_candidate_per_source_unit() {
    js_ast::with_swc_globals(|| {
        // Two separate var-decl source_units each match the hole needle: each is a
        // candidate.
        let chunk = module("const a = \"abc\";\nconst b = \"abc\";\n");
        let selector = member(
            "const DECLARATORS_BEFORE = null, c = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
             DECLARATORS_AFTER = null;",
            Some("c"),
        );
        assert_eq!(member_names(&chunk, &selector), ["a", "b"]);
    });
}

#[test]
fn chunk_resolver_single_declarator_target_window() {
    js_ast::with_swc_globals(|| {
        // A contiguous two-statement window: a helper function then a
        // single-declarator var-decl target living inside a multi-declarator
        // source_unit. Only per-declarator matching within the window finds it.
        let chunk = module(
            "function helper(n) { return n + 1; }\n\
             const lead = 0, theTarget = makeThing(), tail = 1;\n\
             const other = 9;\n",
        );
        let selector = member(
            "function f(x) { return x + 1; }\nconst t = makeThing();",
            Some("t"),
        );
        assert_eq!(member_names(&chunk, &selector), ["theTarget"]);
    });
}

#[test]
fn chunk_resolver_single_declarator_target_window_candidate_per_window() {
    js_ast::with_swc_globals(|| {
        // The window shape appears twice: each window is a candidate.
        let chunk = module(
            "function h1(n) { return n + 1; }\nconst a = makeThing();\n\
             function h2(m) { return m + 1; }\nconst b = makeThing();\n",
        );
        let selector = member(
            "function f(x) { return x + 1; }\nconst t = makeThing();",
            Some("t"),
        );
        assert_eq!(member_names(&chunk, &selector), ["a", "b"]);
    });
}

#[test]
fn chunk_resolver_multi_statement_declarator_hole_target() {
    js_ast::with_swc_globals(|| {
        // A two-statement window whose target item is a declarator-hole var-decl
        // with the pinned target declarator first (the corpus `const mR =
        // ANYTHING, DECLARATORS = null;` shape). The general multi-statement
        // path applies: the matcher absorbs the hole, and `declared_bindings[0]`
        // names the anchored-left target source_unit.
        let chunk = module(
            "function helper(n) { return n; }\n\
             const theTarget = compute(), extra = 1, more = 2;\n",
        );
        let selector = member(
            "function f(x) { return x; }\nconst m = ANYTHING, DECLARATORS = null;",
            Some("m"),
        );
        assert_eq!(member_names(&chunk, &selector), ["theTarget"]);
    });
}

#[test]
fn chunk_resolver_anonymous_statement_candidate() {
    js_ast::with_swc_globals(|| {
        let chunk = module("init();\nregister(widget);\nteardown();\n");
        let selector = member("register(ANYTHING);", None);
        // matches exactly the `register(widget);` statement at body index 1.
        assert_eq!(
            anonymous_groups(&chunk, &selector),
            vec![AnonymousGroupMatch {
                body_indices: vec![1],
                free_bindings: BTreeMap::from([("register".to_string(), "register".to_string())]),
            }]
        );
    });
}

fn exact_member(match_source: &str, target_binding: Option<&str>) -> AnonymousStatementSelector {
    AnonymousStatementSelector {
        identifiers: SourceMatchIdentifierMode::Exact,
        ..member(match_source, target_binding)
    }
}

#[test]
fn chunk_resolver_alpha_declarator_hole_member_matches_single_target_group() {
    js_ast::with_swc_globals(|| {
        let chunk = module(
            "const prefix = 0, a = () => b(), b = () => c(), c = () => \"ready\", tail = 1;\n",
        );
        let pattern = "const DECLARATORS_BEFORE = null, Factory = () => b(), Loader = () => c(), Value = () => \"ready\", DECLARATORS_BETWEEN = null, Tail = 1;";
        let member = member(pattern, Some("Loader"));
        let member_matches = ChunkResolver::new(&chunk)
            .member_candidates("test", &parse(&member))
            .expect("alpha member candidates");
        let group_matches =
            group_candidates(&chunk, &group(pattern), &exports(&[("Loader", "Loader")]));

        assert_eq!(member_matches.len(), 1);
        assert_eq!(group_matches.len(), 1);
        assert_eq!(
            member_matches[0].body_idx,
            group_matches[0].bindings["Loader"].body_idx
        );
        assert_eq!(
            member_matches[0].binding,
            group_matches[0].bindings["Loader"].binding
        );
        assert_eq!(
            member_matches[0].free_bindings,
            group_matches[0].free_bindings
        );
    });
}

// The exact-mode identifier candidate discriminator must stay a *sound*
// prefilter: it may prune only declarators/statements that provably cannot
// match. These tests exercise candidate lists that go through the discriminator
// (exact mode, identifier-bearing needles) and assert the correct source_unit is
// still found — i.e. the discriminator never prunes a real match.

#[test]
fn chunk_resolver_exact_ident_discriminator_finds_identifier_only_decl() {
    js_ast::with_swc_globals(|| {
        // The perf-corpus shape: identifier-only inits with no literal/prop token
        // to pin, in exact mode, so *only* the exact-mode identifier discriminator
        // narrows the declarator scan. The needle (binding `target` + referenced
        // `dep_b`) must reach exactly the matching declarator, not be pruned away.
        // (Exact mode compares the binding name too, so the needle names the real
        // binding, exactly as the source_match rewrite does.)
        let chunk = module(
            "const dep_a = base();\nconst dep_b = base();\n\
             const target = wrap(dep_b);\nconst other = wrap(dep_a);\n",
        );
        let selector = exact_member("const target = wrap(dep_b);", Some("target"));
        assert_eq!(member_names(&chunk, &selector), ["target"]);
    });
}

#[test]
fn chunk_resolver_exact_ident_discriminator_keeps_match_when_referenced_name_is_decisive() {
    js_ast::with_swc_globals(|| {
        // Two declarators share the binding name shape but differ only in the
        // referenced identifier; in exact mode the reference is compared
        // byte-for-byte. The discriminator must keep the declarator whose
        // reference matches (`dep_b`) — a wrongful prune would surface as no
        // candidate, not a wrong source_unit.
        let chunk = module("const m = wrap(dep_a);\nconst m2 = wrap(dep_b);\n");
        let selector = exact_member("const m2 = wrap(dep_b);", Some("m2"));
        assert_eq!(member_names(&chunk, &selector), ["m2"]);
    });
}

#[test]
fn chunk_resolver_exact_ident_anonymous_statement_candidate() {
    js_ast::with_swc_globals(|| {
        // The no-prebind single-statement scan (anonymous) also uses the
        // exact-mode identifier discriminator: `register(widget)` must reach
        // the statement carrying both `register` and `widget`.
        let chunk = module("init();\nregister(other);\nregister(widget);\n");
        let selector = exact_member("register(widget);", None);
        let body_indices: Vec<Vec<usize>> = anonymous_groups(&chunk, &selector)
            .into_iter()
            .map(|group| group.body_indices)
            .collect();
        assert_eq!(body_indices, vec![vec![2]]);
    });
}

#[test]
fn chunk_resolver_exact_declarator_hole_candidate_despite_target_rename() {
    js_ast::with_swc_globals(|| {
        // The declarator-hole path *prebinds* the target name, alpha-coupling it
        // to the candidate's binding — so the exact-mode discriminator must NOT
        // require the needle's target identifier (`c`) of the subject. The source_unit
        // binds `theClass`, not `c`; the prebind path passes
        // `allow_exact_ident = false`, so the match is still found.
        let chunk = module("const p = 1, theClass = \"abc\", q = 2;\nconst other = 5;\n");
        let selector = exact_member(
            "const DECLARATORS_BEFORE = null, c = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
             DECLARATORS_AFTER = null;",
            Some("c"),
        );
        assert_eq!(member_names(&chunk, &selector), ["theClass"]);
    });
}
